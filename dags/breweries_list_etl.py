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
from pyspark.sql import SparkSession
from pyspark.sql.functions import col, upper, when, regexp_replace
from pyspark.sql.types import StructType, StructField, StringType, DecimalType

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

def fetch_and_clean_data(ti):
    # Define the path in MinIO where the data must be read from (bucket: 'bronze')
    tz_brasilia = pytz.timezone("America/Sao_Paulo")
    current_date = datetime.now(tz_brasilia)
    current_year = current_date.strftime("%Y")
    current_month = current_date.strftime("%m")
    current_day = current_date.strftime("%d")

    minio_path = f"{current_year}/{current_month}/{current_day}/breweries_{current_year}{current_month}{current_day}.csv"

    # Get the data from the csv file
    try:
        bronze_data = minio_client.get_object(
            bucket_name='bronze',
            object_name=minio_path,
        )
        logging.info("Data successfully read from bronze bucket")
    except:
        logging.error("Failed to get the data from the bronze bucket") 

    # Create a spark session
    spark = SparkSession.builder.appName("BreweryDataProcessing").master("spark://spark:7077").getOrCreate()

    # Set the schema for the dataframe
    schema = StructType([
        StructField("id", StringType()),
        StructField("name", StringType()),
        StructField("brewery_type", StringType()),
        StructField("address_1", StringType()),
        StructField("address_2", StringType()),
        StructField("address_3", StringType()),
        StructField("city", StringType()),
        StructField("state_province", StringType()),
        StructField("postal_code", StringType()),
        StructField("country", StringType()),
        StructField("longitude", DecimalType(10, 8)),
        StructField("latitude", DecimalType(10, 8)),
        StructField("phone", StringType()),
        StructField("website_url", StringType()),
        StructField("state", StringType()),
        StructField("street", StringType())
    ])

    # Read data from csv format
    df = spark.read.schema(schema).csv(bronze_data)

    # 1 - Remove any entries that have null 'id' column (which is the primary key)
    df = df.filter(df["id"].isNotNull())

    # 2 - Standardize 'name', 'brewery_type', 'city', 'state_province', 'country' and 'state' to upper case
    df = df.withColumn("name", upper(col("name"))) \
       .withColumn("brewery_type", upper(col("brewery_type"))) \
       .withColumn("city", upper(col("city"))) \
       .withColumn("state_province", upper(col("state_province"))) \
       .withColumn("country", upper(col("country"))) \
       .withColumn("state", upper(col("state")))
    
    # 3 - Substitute null values by 'UNKNOWN' in the columns 'brewery_type', 'city', 'state_province', 'country' and 'state'
    df = df.withColumn("brewery_type", when(col("brewery_type").isNull(), "UNKNOWN").otherwise(col("brewery_type"))) \
        .withColumn("city", when(col("city").isNull(), "UNKNOWN").otherwise(col("city"))) \
        .withColumn("state_province", when(col("state_province").isNull(), "UNKNOWN").otherwise(col("state_province"))) \
        .withColumn("country", when(col("country").isNull(), "UNKNOWN").otherwise(col("country"))) \
        .withColumn("state", when(col("state").isNull(), "UNKNOWN").otherwise(col("state")))

    # 4 - Clean 'phone' column removing any special character
    df = df.withColumn("phone", regexp_replace(col("phone"), "[^0-9]", ""))

    # Push the DataFrame to XCom
    ti.xcom_push(key='transformed_data', value=df)

def partition_and_load_data(ti):
    df = ti.xcom_pull(key='transformed_data', task_ids='clean_data')

    # Partition the dataframe by 'country' and 'state'
    country_state_list = df.select("country","state").distinct().collect()

    for row in country_state_list:
        country = row["country"]
        state = row["state"]
        
        # Filter specific data
        df_partition = df.filter((col("country") == country) & (col("state") == state))

        # Convert data to parquet format
        buffer = io.BytesIO()
        df_partition.write.parquet(buffer)
        buffer.seek(0)

        # Set path in MinIO silver bucket 
        minio_path = f"silver/{country}/{state}/breweries.parquet"

        # Save file to MinIO
        try:
            minio_client.put_object(
                bucket_name="silver",
                object_name=minio_path,
                data=buffer,
                length=buffer.getbuffer().nbytes,
                content_type="application/parquet"
            )
            logging.info(f"Data of size {len(buffer.getbuffer().nbytes)} bytes was successfully saved to {minio_path}")
        except Exception as e:
            logging.error(f"Could not save the data to MinIO: {e}")



# Declare DAG
default_args = {
    "owner": "airflow",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

dag = models.DAG(
    dag_id='ETL_breweries_list',
    description= 'DAG to ...',
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

clean_data = PythonOperator(
    task_id='clean_data',
    python_callable=fetch_and_clean_data,
    dag=dag,
)

load_to_silver = PythonOperator(
    task_id='load_to_silver_layer',
    python_callable=partition_and_load_data,
    dag=dag,
)

end = BashOperator(
    task_id="pipeline_end",
    bash_command='echo "PIPELINE ENDED"; sleep 15',
    dag=dag,
    trigger_rule="all_done"
)

# Tasks dependencies
start >> extract_breweries_listing >> load_to_bronze >> clean_data >> load_to_silver >> end