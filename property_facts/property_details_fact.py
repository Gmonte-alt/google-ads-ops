# file name:
# version:
# Notes:

import pandas as pd
import sqlite3
import snowflake.connector

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

# SQLite database path
sqlite_db_path = 'ff_snowflake.db'


# Define the query
query = """
WITH photo_count AS (
    SELECT
    property_id,
    try_to_numeric(substr(property_id, 1, charindex('_', property_id)-1)) property,
    try_to_numeric(substr(property_id, charindex('_', property_id)+1, LEN(property_id))) unit_num,
    CASE
      WHEN COUNT(photo_path) >= 2 THEN TRUE ELSE FALSE
      END AS has_min_photo_count_flag,
      COUNT(photo_path) AS photo_count
    FROM
      prod_furnishedfinder_dwh.ff_aaronh_ffmaps.photos
      WHERE _fivetran_deleted=FALSE
    GROUP BY all
)
, clean_tp AS (
SELECT *
FROM prod_furnishedfinder_dwh.clean.temp_properties
WHERE property_paid_flag=1
)
  
SELECT 
     tp.temp_property_id
     , tp.tp_live_id
     , p.property_id
     , pu.unit_id
     , pu.unit_num
     , CASE WHEN p.property_id IS NOT NULL 
           THEN CONCAT(p.property_id::VARCHAR,'_',COALESCE(pu.unit_num::VARCHAR,'1'))
           ELSE CONCAT('TEMP',tp.temp_property_id::VARCHAR)
         END AS listing_id
     , CASE WHEN p.property_id IS NOT NULL THEN 'properties' ELSE 'temp_properties' END AS listing_id_source
     , p.property_created_at
     , tp.property_created_at AS temp_property_created_at
     , COALESCE(p.id_verified,tp.id_verified) AS id_verified
     , COALESCE(p.id_verified_flag,tp.id_verified_flag) as id_verified_flag
     , COALESCE(p.active_flag,tp.active_flag) as active_flag
     , COALESCE(p.listing_setup_at,tp.listing_setup_at) as listing_setup_at
     , COALESCE(p.property_traveler_max,tp.property_traveler_max) as property_traveler_max
     , COALESCE(p.longitude,tp.longitude) as longitude
     , COALESCE(p.latitude,tp.latitude) as latitude
     , COALESCE(p.property_city,tp.property_city) as property_city
     , COALESCE(p.property_state,tp.property_state) as property_state
     , COALESCE(p.property_country,tp.property_country) as property_country
     , COALESCE(p.property_address,tp.property_address) as property_address
     , COALESCE(p.neighborhood_overview,tp.neighborhood_overview) as neighborhood_overview
     , COALESCE(p.property_postal_code,tp.property_postal_code) as property_postal_code
     , COALESCE(p.availability_updated_at,tp.availability_updated_at) as availability_updated_at
     , COALESCE(p.renewal_date,tp.renewal_date) as renewal_date
     , COALESCE(p.property_paid_flag,tp.property_paid_flag) as property_paid_flag
     , COALESCE(p.property_source,tp.property_source) as property_source
     , COALESCE(p.sublet_flag,tp.sublet_flag) as sublet_flag
     , COALESCE(p.term_min,tp.term_min) as term_min
     , COALESCE(p.term_max,tp.term_max) as term_max
     , COALESCE(p.accept_credit_card_flag,tp.accept_credit_card_flag) as accept_credit_card_flag
     , COALESCE(p.property_name,tp.property_name) as property_name

     -- New fields from prop_clean
     , COALESCE(p.furnished_flag,tp.furnished_flag) as furnished_flag
     , COALESCE(p.utilities_flag,tp.utilities_flag) as utilities_flag
     , COALESCE(p.property_payment_id,tp.property_payment_id) as property_payment_id
     , COALESCE(p.property_updated_at,tp.property_updated_at) as property_updated_at
        
   
     -- Property_unit
     , pu.unit_created_at
     , pu.first_live_date  -- unit_prefix needed?
     , pu.unit_available_at
     , pu.do_not_renew_flag as unit_do_not_renew_flag
     , pu.unit_hidden_flag
     , pu.unit_squareft
     , pu.unit_rent_min
     , pu.unit_bedrooms
     , pu.unit_bathrooms
     , pu.unit_furnished_flag
     , pu.unit_utilities_flag
     , pu.unit_entrance_type
     , pu.unit_internetspeed
     , pu.unit_pets_flag
     , pu.unit_accommodations
     , pu.unit_bath_type
     , pu.unit_tv_description
     , pu.unit_laundry_flag
     , pu.unit_bed_object
     , pu.unit_sleep_object
 
     -- Photo CTE   
     , COALESCE(ph.photo_count,0) photo_count
     , COALESCE(ph.has_min_photo_count_flag, FALSE) AS has_min_photo_count_flag
    
     -- Mozart.Properties & PCF field adoption
     , concat('https://www.furnishedfinder.com/property/',coalesce(p.property_id::VARCHAR,''),'_', coalesce(pu.unit_num::varchar,'1')) AS unit_url 
     , CASE WHEN COALESCE(p.property_city,tp.property_city) ILIKE'Barrow'
         AND COALESCE(p.property_state,tp.property_state) ILIKE 'Alaska' 
         THEN TRUE else FALSE 
         END AS test_property_flag
     , CASE 
         WHEN COALESCE(p.property_paid_flag,tp.property_paid_flag) 
         AND COALESCE(p.id_verified_flag,tp.id_verified_flag)
         AND COALESCE(p.active_flag,tp.active_flag)
         AND p.property_id IS NOT NULL
         THEN TRUE ELSE FALSE END AS know_your_customer_flag
     , CASE 
          WHEN COALESCE(p.renewal_date,tp.renewal_date) <= current_date 
          THEN TRUE ELSE FALSE 
          END property_expired_flag
     , CASE 
         WHEN p.property_paid_flag THEN TRUE 
         WHEN tp.property_paid_flag AND tp.renewal_date> current_date 
         THEN TRUE ELSE FALSE END current_property_paid_flag    
     , CASE 
         WHEN p.availability_updated_at>= DATEADD(day, -45, current_date) 
         THEN TRUE ELSE FALSE 
         END AS property_availability_updated_at_flag
     , CASE 
         WHEN pu.unit_available_at>= DATEADD(day, -45, current_date) 
         THEN TRUE ELSE FALSE 
         END AS unit_availability_updated_at_flag
     , CASE
         WHEN (pu.unit_rent_min>0 OR p.property_type='Hotel')
         THEN TRUE ELSE FALSE 
         END AS rent_min_flag
     , CASE
         WHEN property_availability_updated_at_flag
         OR unit_availability_updated_at_flag
         OR p.property_type='Hotel'
         THEN TRUE ELSE FALSE END AS availability_flag
     , CASE
         WHEN ph.has_min_photo_count_flag
         AND p.property_city IS NOT NULL
         AND p.property_state IS NOT NULL
         AND rent_min_flag
         THEN TRUE ELSE FALSE END AS min_content_flag
     , CASE
         WHEN COALESCE(p.property_paid_flag,tp.property_paid_flag) 
         AND COALESCE(p.active_flag,tp.active_flag)
         AND COALESCE(p.id_verified_flag,tp.id_verified_flag)
         AND min_content_flag
         AND availability_flag 
         THEN TRUE ELSE FALSE
         END AS property_live_status_flag
     , CASE 
        WHEN p.property_units ILIKE 'Complex' THEN 'Complex'
        WHEN TRIM(p.property_type) ILIKE '%room%' then 'Room'
        WHEN TRIM(p.property_type) ILIKE 'hotel' then 'Hotel'
        ELSE 'Entire Place'
       END AS property_type_group
     -- Add additional fields as necessary from your query
     , sysdate() AS last_processed_at
FROM 
    prod_furnishedfinder_dwh.clean.properties p 
    FULL OUTER JOIN clean_tp tp ON p.property_id=tp.tp_live_id
    LEFT JOIN prod_furnishedfinder_dwh.clean.property_units pu ON p.property_id=pu.property_id
    LEFT JOIN photo_count ph ON ph.property=pu.property_id AND ph.unit_num=pu.unit_num
WHERE 
    1=1 AND p.PROPERTY_ID IS NOT NULL
"""

# Execute the query and fetch the data
df = pd.read_sql(query, conn_snowflake)

# Close the Snowflake connection
conn_snowflake.close()

# Connect to SQLite database (or create it if it doesn't exist)
sqlite_conn = sqlite3.connect(sqlite_db_path)

# Define table name for storing Snowflake data in SQLite
table_name = 'property_listing_data'

# Insert data into SQLite
df.to_sql(table_name, sqlite_conn, if_exists='replace', index=False)

# Commit and close the SQLite connection
sqlite_conn.commit()
sqlite_conn.close()

print(f"Data successfully imported into SQLite table '{table_name}'.")