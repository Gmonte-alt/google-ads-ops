import snowflake.connector
import pandas as pd
from sqlalchemy import create_engine

# Connect to Snowflake and query the respective tables
conn_snowflake = snowflake.connector.connect(
    user='GILBERT_MONTEMAYOR', # '<your_user>', # GILBERT_MONTEMAYOR
    password= 'Yaniandgilby2008%', # '<your_password>', # Yaniandgilby2008%
    account='gz04952.us-east-2.aws', # '<your_account>', # MOZARTDATA.MOZART_FURNISHED_FINDER
    warehouse='PROD_FURNISHEDFINDER_DWH', # '<your_warehouse>', # PROD_FURNISHEDFINDER_DWH
    role='FF_READONLY'
    # database='<your_database>', 
    # schema='<your_schema>'
)

# Define your SQL query
sql_query = """
WITH DuplicateRequests AS (
    SELECT 
        TRAVELER_EMAIL,
        MOVE_IN_DATE,
        MOVE_OUT_DATE,
        LISTING_ID,
        BOOKING_REQUEST_ID,
        SUBMITTED_AT,
        ROW_NUMBER() OVER (PARTITION BY TRAVELER_EMAIL, MOVE_IN_DATE, LISTING_ID 
                           ORDER BY SUBMITTED_AT DESC) AS request_rank
    FROM 
        PROD_FURNISHEDFINDER_DWH.MOZART.BOOKING_REQUESTS
),
BookingWithScenario1 AS (
    SELECT 
        BR.*,
        CASE 
            WHEN DR.request_rank = 1 THEN 'confirmed'
            ELSE 'duplicate'
        END AS Scenario1_Status
    FROM 
        PROD_FURNISHEDFINDER_DWH.MOZART.BOOKING_REQUESTS BR
    LEFT JOIN 
        DuplicateRequests DR 
    ON 
        BR.BOOKING_REQUEST_ID = DR.BOOKING_REQUEST_ID
),
MultipleListingsRequests AS (
    SELECT 
        TRAVELER_EMAIL,
        MOVE_IN_DATE,
        MOVE_OUT_DATE,
        LISTING_ID,
        ROW_NUMBER() OVER (PARTITION BY TRAVELER_EMAIL, MOVE_IN_DATE, MOVE_OUT_DATE ORDER BY BOOKING_REQUEST_ID DESC) AS LISTING_RANK
    FROM 
        BookingWithScenario1
    WHERE Scenario1_Status IS DISTINCT FROM 'duplicate'
),
BookingWithScenario3 AS (
    SELECT 
        BWS1.*,
        CASE 
            WHEN MLR.LISTING_RANK = 1 THEN 'confirmed'
            ELSE 'unconfirmed'
        END AS Scenario3_Status
    FROM 
        BookingWithScenario1 BWS1
    LEFT JOIN 
        MultipleListingsRequests MLR
    ON 
        BWS1.TRAVELER_EMAIL = MLR.TRAVELER_EMAIL
        AND BWS1.MOVE_IN_DATE = MLR.MOVE_IN_DATE
        AND BWS1.MOVE_OUT_DATE = MLR.MOVE_OUT_DATE
        AND BWS1.LISTING_ID = MLR.LISTING_ID
),
OverlappingRequests AS (
    SELECT 
        BR1.TRAVELER_EMAIL,
        BR1.BOOKING_REQUEST_ID AS REQUEST_ID1,
        BR1.SUBMITTED_AT AS SUBMITTED_AT1,
        BR2.BOOKING_REQUEST_ID AS REQUEST_ID2,
        BR2.SUBMITTED_AT AS SUBMITTED_AT2,
        BR1.MOVE_IN_DATE,
        BR1.MOVE_OUT_DATE,
        BR1.LISTING_ID,
        BR1.CITY,
        ROW_NUMBER() OVER (PARTITION BY BR1.LISTING_ID, BR1.MOVE_IN_DATE, BR1.MOVE_OUT_DATE ORDER BY BR2.SUBMITTED_AT ASC) AS OVERLAP_RANK
    FROM 
        BookingWithScenario3 BR1
    JOIN 
        BookingWithScenario3 BR2 
    ON 
        BR1.LISTING_ID = BR2.LISTING_ID
        AND BR1.TRAVELER_EMAIL <> BR2.TRAVELER_EMAIL
        AND BR1.MOVE_IN_DATE <= BR2.MOVE_OUT_DATE 
        AND BR1.MOVE_OUT_DATE >= BR2.MOVE_IN_DATE
),
FilteredOverlappingRequests AS (
    SELECT 
        ORQ.TRAVELER_EMAIL,
        ORQ.REQUEST_ID1,
        ORQ.SUBMITTED_AT1,
        ORQ.LISTING_ID,
        ORQ.MOVE_IN_DATE,
        ORQ.MOVE_OUT_DATE,
        CASE 
            WHEN ORQ.SUBMITTED_AT1 < ORQ.SUBMITTED_AT2 THEN 'confirmed'
            ELSE 'declined'
        END AS Scenario2_Status,
        ORQ.OVERLAP_RANK,
        ORQ.CITY
    FROM 
        OverlappingRequests ORQ
    WHERE 
        ORQ.OVERLAP_RANK = 1
)
SELECT 
    BWS3.SUBMITTED_DATE,
    BWS3.SUBMITTED_AT,
    BWS3.TRAVELER_OCCUPATION,
    BWS3.CITY,
    BWS3.PSTATE,
    BWS3.MOVE_IN_DATE,
    BWS3.MOVE_OUT_DATE,
    BWS3.PROPERTY_ID,
    BWS3.UNIT_NUM,
    BWS3.UNIT_ID,
    BWS3.LISTING_ID,
    BWS3.NUMBER_OF_OCCUPANTS,
    BWS3.SOURCE,
    BWS3.BUDGET,
    BWS3.PETS,
    BWS3.BOOKING_REQUEST_ID,
    BWS3.LAST_PROCESSED_AT,
    BWS3.SCENARIO1_STATUS,
    BWS3.SCENARIO3_STATUS,
    FORQ.Scenario2_Status,
    CASE 
        WHEN BWS3.Scenario1_Status = 'duplicate' THEN BWS3.Scenario1_Status
        WHEN FORQ.Scenario2_Status IS NOT NULL THEN FORQ.Scenario2_Status
        WHEN BWS3.Scenario3_Status = 'confirmed' THEN BWS3.Scenario3_Status
        ELSE BWS3.Scenario1_Status
    END AS Final_Status
FROM 
    BookingWithScenario3 BWS3
LEFT JOIN 
    FilteredOverlappingRequests FORQ 
ON 
    BWS3.BOOKING_REQUEST_ID = FORQ.REQUEST_ID1;
"""

# Execute the SQL query and load the result into a DataFrame
df = pd.read_sql(sql_query, conn_snowflake)

# Close the Snowflake connection
conn_snowflake.close()

# Save the DataFrame to SQLite
sqlite_engine = create_engine('sqlite:///ff_snowflake.db')
df.to_sql('booking_request_fact', sqlite_engine, if_exists='replace', index=False)

print("Data saved to SQLite database 'ff_snowflake.db' in the table 'booking_request_fact'.")
