# Breweries Data: Building a open-source and local ETL pipeline using Docker from scratch
In this project I constructed a simple ETL data pipeline following the medallion architecture. The main goal was to fetch breweries' information from a public API, apply tranformations to that data and store each stage of the data to one layer (bronze -> silver -> gold). The solution design was not meant to be robust, ready for production. 
It was rather exploratory, focusing on exploring open-source tools and manage to run the end-to-end pipeline locally.

## 1) Solution Architecture

![medallion_architecture](https://github.com/user-attachments/assets/32dac85c-5025-48e6-a64d-da10142cb100)

Having in mind that the data volume was not so big, the MinIO datalake was chosen, that would be lighter to run locally in comparinson to Hadoop HDSF. AS for the orchestrator, Airflow has the advantages of being a practical open-source solution. As for the structural database, PostgreSQL was a no-brainer, since it is also needed to run the Airflow image in Docker. Lastly, docker was used to provide the whole local environment to develop the solution, and aftewards share it easily with the community.

## 2) Data Trasnformation

Following the principles of the medallion architecture, the DAGs on Airflow were ordered to incrementally clean and organize the data fetched. All the DAGs are in the *'breweries_list_etl.py'* file within the 'dags' folder.

![DAG_airflow](https://github.com/user-attachments/assets/49ecae1c-c401-4487-836e-3a378ae0186f)


- *start_pipeline:* simple DAG to record the begin of the ETL pipeline
- *extract_from_api:* it will use the API's endpoint to fetch the raw data as a json variable and after that save it to a pandas dataframe. To handle possible API connection errors, many try-except pairs were used.
- *load_to_bronze_layer:* making use of the cross-communication inbetween DAGs (XCom) it will get the dataframe from the previous step and persist it in the .csv format in the bronze bucket from MinIO. To organize the historical data, each day has its folder following the structure *'bronze/year/month/day/csv_file_yyyyMMdd.csv'*.
- *clean_and_load_to_silver_layer:* here the columns that were previouly consumed as string type are converted to their most suitable type. For the latitude and longitude, I casted them as decimal number with 8 decimal places, since the eight place is already a very high precision (in the order of mm). The following transformations are performed on the data:
  - Remove entries with null 'id' columns (primary key)
  - Standardize the columns "name", "brewery_type", "city", "state_province", "country", "state" with upper case
  - Replace null values by 'UNKNOWN' in the columns "brewery_type", "city", "state_province", "country", "state"
  - Remove non-numerical characters from the "phone" columns, leaving only numbers
  After these transformations the data was partitioned by country and state, and saved into the silver layer as parquet, following the struture *'silver/country/state/breweries.parquet'*
- *create_view_and_load_to_gold:* after storing the data patitioned by country and state, in this step I loop through the files combining their data to perform a group by operation in the end withrespect to country, state, and brewery type. And to finish I count the number of breweries within each partition. the resultant table is stored as parquet in the gold layer of MinIO data lake.
- *create_table:* this is a simple DAG built by PostgresOperator to create the table *'brewery_type_per_location.sql'* in the SQL database.
- *load_view_into_postgres:* uses the aggregated dataframe as source for a loop of "INSERT INTO" commands, that fill the table lines. Below is the resultant table on Pgadmin, with
- ![view_postgres](https://github.com/user-attachments/assets/01563f19-1cab-46f4-b487-c4e4481f851c)
