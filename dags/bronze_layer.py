import requests
from requests import Response
from datetime import datetime, timedelta
import pytz
import json
from pyspark.sql import SparkSession
import pyspark.sql.functions as F
from pyspark.sql.types import StructType, StructField, StringType
from airflow import models
from airflow.operators.python import PythonOperator
from airflow.operators.bash import BashOperator

# 1) Extract the data from the API
def extract_from_api(api_endpoint):
    # Create a Spark session
    spark = SparkSession.builder \
        .appName("API to MinIO") \
        .config("spark.hadoop.fs.s3a.endpoint", "http://localhost:9000") \
        .config("spark.hadoop.fs.s3a.access.key", "minioadmin") \
        .config("spark.hadoop.fs.s3a.secret.key", "minioadmin123") \
        .config("spark.hadoop.fs.s3a.path.style.access", "true") \
        .config("spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem") \
        .getOrCreate()
    
    # Define the schema of the dataframe for the API

    schema = StructType([
        StructField("id", StringType(), False),
        StructField("name", StringType(), True),
        StructField("brewery_type", StringType(), True),
        StructField("address_1", StringType(), True),
        StructField("address_2", StringType(), True),
        StructField("address_3", StringType(), True),
        StructField("city", StringType(), True),
        StructField("state_province", StringType(), True),
        StructField("postal_code", StringType(), True),
        StructField("country", StringType(), True),
        StructField("longitude", StringType(), True),  
        StructField("latitude", StringType(), True),   
        StructField("phone", StringType(), True),
        StructField("website_url", StringType(), True),
        StructField("state", StringType(), True),
        StructField("street", StringType(), True)
    ])
    
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
                    print("Breweries list is not empty (SUCCESS)!")
                    
                    # Create a Spark DataFrame safely
                    try:
                        breweries_df = spark.createDataFrame(breweries_data, schema=schema)
                        print(f"\nNumber of breweries: {breweries_df.count()}")
                    except Exception as e:
                        print(f"Schema mismatch or Spark DataFrame error: {e}")

                else:
                    print("Failed to get breweries list (FAIL)! Empty or malformed JSON response.")
            
            except json.JSONDecodeError:
                print("Error: API response is not valid JSON.")

        else:
            print(f"Error: API request failed with status code {response.status_code}")

    except requests.RequestException as e:
        print(f"Error: Failed to connect to API ({e})")
    
    return breweries_df

# 2) Load extracted data into the bronze layer in the Minio Datalake

def load_to_minio(df):
    # Get current date to save the file in the proper folder
    tz_brasilia = pytz.timezone("America/Sao_Paulo")
    current_date = datetime.now(tz_brasilia)
    current_year = current_date.strftime("%Y")
    current_month = current_date.strftime("%m")
    current_day = current_date.strftime("%d")

    # Define the path in MinIO where the data must be persisted (bucket: 'bronze')
    minio_path = f"s3a://bronze/{current_year}/{current_month}/{current_day}/breweries_{current_year}{current_month}{current_day}.parquet"

    # Save dataframe into MinIO in Parquet format
    df.write.mode("overwrite").parquet(minio_path)

def arguments():
    extract = extract_from_api("https://api.openbrewerydb.org/breweries")
    load_to_minio(extract)

# Declare DAG
dag = models.DAG(
    dag_id='ETL_breweries_list',
    description= 'DAG to ...',
    schedule="0 04 * * *",
    start_date = datetime.today() - timedelta(days=1),
    catchup=False,
    tags=["LIVE"],
    retries= 1,
    retry_delay= timedelta(minutes=5),
)

# Operators
start = BashOperator(
    task_id="start_pipeline",
    bash_command='echo "START PIPELINE"; sleep 15',
    dag=dag,
)

extract_breweries_listing = PythonOperator(
    task_id='extract_and_load_to_bronze',
    python_callable=arguments,
    dag=dag,
)

end = BashOperator(
    task_id="pipeline_end",
    bash_command='echo "PIPELINE ENDED"; sleep 15',
    dag=dag,
    trigger_rule="all_done"
)

# Tasks dependencies
start >> extract_breweries_listing >> end