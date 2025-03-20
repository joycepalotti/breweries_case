import requests
from datetime import datetime, timedelta
import pytz
import json
import pandas as pd
from minio import Minio
import io
import logging
from airflow import models
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator
from airflow.exceptions import AirflowException
from airflow.providers.postgres.operators.postgres import PostgresOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook

minio_client = Minio(
    "minio:9000",
    access_key="minioadmin",
    secret_key="minioadmin123",
    secure=False
)

# 1) Extract the data from the API
def extract_from_api(api_endpoint, ti):
      
    try:
        # Make the GET request
        response = requests.get(api_endpoint, timeout=10)

        # Check if request was successful (HTTP 200 OK)
        if response.status_code == 200:
            try:
                # Attempt to parse JSON response
                breweries_data = response.json()
                
                # Ensure the response is not empty and contains valid data
                if isinstance(breweries_data, list) and len(breweries_data) > 0 and 'id' in breweries_data[0]:
                    logging.info("Breweries list is not empty (SUCCESS)!")
                    
                    # Create a Spark DataFrame safely
                    try:
                        breweries_df = pd.DataFrame(breweries_data)
                        logging.info(f"\nNumber of breweries: {breweries_df.count()}")
                        
                    except Exception as e:
                        logging.error(f"Error when creating DataFrame: {e}")
                        
                else:
                    logging.error("Failed to get breweries list (FAIL)! Empty or malformed JSON response.")
                    
            
            except json.JSONDecodeError:
                logging.error("Error: API response is not valid JSON.")
                

        else:
            logging.error(f"Error: API request failed with status code {response.status_code}")
            

    except requests.RequestException as e:
        logging.error(f"Error: Failed to connect to API ({e})")

    # Push the DataFrame to XCom
    ti.xcom_push(key='bronze_data', value=breweries_df)

# 2) Load extracted data into the bronze layer in the Minio Datalake

def load_to_minio(ti):
    df = ti.xcom_pull(key='bronze_data', task_ids='extract_from_api')

    # Check if dataframe contains data
    if df is None or df.empty:
        logging.error("Dataframe is empty")
    else:
        csv_data = df.to_csv(index=False).encode('utf-8')
    
    # Get current date to save the file in the proper folder
    tz_brasilia = pytz.timezone("America/Sao_Paulo")
    current_date = datetime.now(tz_brasilia)
    current_year = current_date.strftime("%Y")
    current_month = current_date.strftime("%m")
    current_day = current_date.strftime("%d")

    # Define the path in MinIO where the data must be persisted (bucket: 'bronze')
    minio_path = f"{current_year}/{current_month}/{current_day}/breweries_{current_year}{current_month}{current_day}.csv"
    logging.info(f"Path where the file will be saved:{minio_path}")

    # Save dataframe into MinIO in Parquet format
    try:
        minio_client.put_object(
            bucket_name='bronze',
            object_name=minio_path,
            data=io.BytesIO(csv_data),
            length=len(csv_data),
            content_type='application/csv'
        )
        logging.info(f"Data of size {len(csv_data)} bytes was successfully saved to {minio_path}")
    except Exception as e:
        logging.error(f"Could not save the data to MinIO: {e}")

# 3) Transform data to parquet and partition it by brewery location

def clean_and_load_data():
    # Get current date based on Sao Paulo timezone
    tz_brasilia = pytz.timezone("America/Sao_Paulo")
    current_date = datetime.now(tz_brasilia)
    current_year = current_date.strftime("%Y")
    current_month = current_date.strftime("%m")
    current_day = current_date.strftime("%d")

    minio_path = f"{current_year}/{current_month}/{current_day}/breweries_{current_year}{current_month}{current_day}.csv"

    # Get the data from MinIO bronze bucket
    try:
        response = minio_client.get_object(bucket_name='bronze', object_name=minio_path)
        df = pd.read_csv(response)
        logging.info("Data successfully read from bronze bucket")
    except Exception as e:
        logging.error(f"Failed to get the data from the bronze bucket: {e}")
        return

    # 1 - Remove entries with null 'id'
    df = df.dropna(subset=["id"])

    # 2 - Standirdize some columns with upper case
    cols_to_upper = ["name", "brewery_type", "city", "state_province", "country", "state"]
    df[cols_to_upper] = df[cols_to_upper].apply(lambda x: x.str.upper())

    # 3 - Replace null values by 'UNKNOWN' in some columns
    df.fillna({"brewery_type": "UNKNOWN", "city": "UNKNOWN", "state_province": "UNKNOWN", "country": "UNKNOWN", "state": "UNKNOWN"}, inplace=True)

    # 4 - Clean up the 'phone' columns, removing non-numerical characters
    df["phone"] = df["phone"].astype(str).str.replace(r"[^0-9]", "", regex=True)

    # Lis country/state possible combinations
    country_state_groups = df.groupby(["country", "state"])

    for (country, state), group_df in country_state_groups:
        buffer = io.BytesIO()
        group_df.to_parquet(buffer, index=False)
        buffer.seek(0)

        # Define MinIO's path (silver bucket)
        minio_path = f"silver/{country}/{state}/breweries.parquet"

        # Salvar arquivo no MinIO
        try:
            minio_client.put_object(
                bucket_name="silver",
                object_name=minio_path,
                data=buffer,
                length=buffer.getbuffer().nbytes,
                content_type="application/parquet"
            )
            logging.info(f"Data saved successfully to {minio_path}")
        except Exception as e:
            logging.error(f"Could not save the data to MinIO: {e}")

# 4) Fetch the data from the silver bucket and create the aggregated view
def create_aggregated_view(ti):
    try:
        # List all the objects inside the "silver" bucket
        objects = minio_client.list_objects(bucket_name="silver", recursive=True)

        # List to store all DataFrames
        df_list = []

        # Iterate over the object list and read Parquet files
        for obj in objects:
            if obj.object_name.endswith(".parquet"):
                response = minio_client.get_object(bucket_name="silver", object_name=obj.object_name)
                data = io.BytesIO(response.read())
                df = pd.read_parquet(data)
                df_list.append(df)
                logging.info(f"Fetched {obj.object_name}")

        # Check if we loaded any data
        if not df_list:
            logging.warning("No data found in the silver bucket")
            return

        # Concatenate all dataframes into one
        aggregated_df = pd.concat(df_list, ignore_index=True)

        # Create aggregated view: count breweries per country/state
        aggregated_view = aggregated_df.groupby(["country", "state", "brewery_type"]) \
                                       .size() \
                                       .reset_index(name="brewery_count")

        # Convert to Parquet format
        buffer = io.BytesIO()
        aggregated_view.to_parquet(buffer, index=False)
        buffer.seek(0)

        # Save the aggregated file to MinIO (gold layer)
        minio_path = "view_aggregated_breweries.parquet"
        minio_client.put_object(
            bucket_name="gold",
            object_name=minio_path,
            data=buffer,
            length=buffer.getbuffer().nbytes,
            content_type="application/parquet"
        )
        logging.info(f"Aggregated view successfully saved to {minio_path}")
        
        # Push the aggregated view to XCom
        ti.xcom_push(key='aggregated_df', value=aggregated_view.to_dict())

    except Exception as e:
        logging.error(f"Failed to create aggregated view: {e}")

# 5) Save view into Postgres database
def store_view_into_postgres(ti):
    try:
        breweries_dict = ti.xom_pull(key='aggregated_df', task_ids='create_view_and_load_to_gold')

        if not breweries_dict:
            logging.error("No data was found in the view")
        
        postgres_hook = PostgresHook(postgres_conn_id='brewery_connection')
        insert_query = """
        INSERT INTO brewery_type_per_location (country, state, brewery_type, brewery_count)
        VALUES (%s, %s, %s, %i)
        ON CONFLICT (country, state, brewery_type) DO UPDATE
        SET brewery_count = EXCLUDED.brewery_count;
        """

        for row in breweries_dict:
            postgres_hook.run(insert_query, parameters=(row['country'], row['state'], row['brewery_type'], row['brewery_count']))

        logging.info("Aggregated data successfully inserted into PostgreSQL")

    except Exception as e:
        logging.error(f"Failed to insert aggregated data: {e}")


# 6) Create a wathcer task to monitor tasks' errors
def watcher(**context):
    ti = context['ti']
    dag_run = ti.get_dagrun()
    failed_tasks = [t for t in dag_run.get_task_instances() if t.state == 'failed']

    if failed_tasks:
        logging.error("A task in the DAG has failed!")
        for task in failed_tasks:
            logging.error(f"Task {task.task_id} failed.")
        raise AirflowException("The DAG failed because one or mores task failed")
    else:
        logging.info("All tasks ran successfully!")

# Declare DAG
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

dag = models.DAG(
    dag_id='ETL_breweries_list',
    description= 'Simple DAG to organize the brewery listing data following a medallion architecture.',
    schedule="0 04 * * *",
    start_date = datetime.today() - timedelta(days=1),
    catchup=False,
    default_args=default_args,
    tags=["LIVE"],
)

# Operators
start = BashOperator(
    task_id="start_pipeline",
    bash_command='echo "START PIPELINE"; sleep 15',
    dag=dag,
)

extract_breweries_listing = PythonOperator(
    task_id='extract_from_api',
    python_callable=extract_from_api,
    op_args=['https://api.openbrewerydb.org/breweries'],
    dag=dag,
)

load_to_bronze = PythonOperator(
    task_id='load_to_bronze_layer',
    python_callable=load_to_minio,
    dag=dag,
)

clean_and_load_to_silver = PythonOperator(
    task_id='clean_and_load_to_silver_layer',
    python_callable=clean_and_load_data,
    dag=dag
)

create_view_and_load_to_gold = PythonOperator(
    task_id='create_view_and_load_to_gold',
    python_callable=create_aggregated_view,
    dag=dag
)

create_table_task = PostgresOperator(
    task_id='create_table',
    postgres_conn_id='brewery_connection',
    sql="""
    CREATE TABLE IF NOT EXISTS brewery_type_per_location (
        country TEXT,
        state TEXT,
        brewery_type TEXT,
        brewery_count INTEGER
    );
    """,
    dag=dag,
)

load_data_to_postgres = PythonOperator(
    task_id='load_view_into_postgres',
    python_callable=store_view_into_postgres,
    dag=dag
)

end = BashOperator(
    task_id="pipeline_end",
    bash_command='echo "PIPELINE ENDED"; sleep 15',
    dag=dag,
    trigger_rule="all_done"
)

monitor_errors = PythonOperator(
    task_id="watcher_task",
    python_callable=watcher,
    provide_context=True,
    dag=dag,
    default_args={"retries":0}
)

# Tasks dependencies
start >> extract_breweries_listing >> load_to_bronze >> clean_and_load_to_silver >> create_view_and_load_to_gold >> create_table_task >> load_data_to_postgres >> monitor_errors >> end
