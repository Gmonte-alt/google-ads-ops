# file name: ga4-reports/query_ga4_database_default-channel-group-fact-table_cleaned.py
# version: V000-000-002
# output: ga4_channel_groups_cleaned sqlite table and csv
# Notes: Deduplicated cross-network vs display channel groupings

import sqlite3
import pandas as pd

# Connect to the SQLite database
conn = sqlite3.connect("ga4_data.db")
cursor = conn.cursor()

# Load the original table
query = '''SELECT * FROM ga4_channel_groups;'''
df = pd.read_sql_query(query, conn)

print(df)
print("-----" * 20)
print(len(df)) 

def deduplicate_display_and_cross_network(df):
    # Filter rows with Default_Channel_Group as 'Display' or 'Cross-network'
    display_cross_df = df[df['Default_Channel_Group'].isin(['Display', 'Cross-network'])]
    print(f"Display and Cross-network Groups - Total Rows: {len(display_cross_df)}")

    # Sort by Campaign and give priority to 'Cross-network'
    display_cross_df = display_cross_df.sort_values(by=['Campaign', 'Default_Channel_Group'], 
                                                    ascending=[True, True])  # 'Cross-network' comes before 'Display'

    # Drop duplicate campaigns, keeping the first occurrence (i.e., 'Cross-network')
    deduplicated_df = display_cross_df.drop_duplicates(subset='Campaign', keep='first')
    print(f"Deduplicated Display and Cross-network Rows: {len(deduplicated_df)}")

    # Return the cleaned DataFrame
    return deduplicated_df

# Function to assign new channel group based on given logic
def assign_new_channel_group(df):
    # Create an empty DataFrame to store the cleaned data
    cleaned_df = pd.DataFrame()

    # Filter for Direct channel group
    direct_df = df[df['Default_Channel_Group'] == 'Direct']
    print(f"Direct Channel Group - Initial Rows: {len(direct_df)}")
    direct_df = direct_df[(direct_df['Source'] == '(direct)') & (direct_df['Medium'] == '(none)') & (direct_df['Campaign'] == '(direct)')]
    print(f"Direct Channel Group - Filtered Rows: {len(direct_df)}")
    direct_df['new_channel_group'] = 'Direct'
    cleaned_df = pd.concat([cleaned_df, direct_df], ignore_index=True)

    # Filter for Email channel group
    email_df = df[df['Default_Channel_Group'] == 'Email']
    print(f"Email Channel Group - Initial Rows: {len(email_df)}")
    email_df = email_df[((email_df['Source'] == 'email') & (email_df['Medium'] == 'marketing')) | ((email_df['Source'] == 'marketing') & (email_df['Medium'] == 'email')) |
                        ((email_df['Source'].str.contains('mail.google.com|outlook.live.com|webmail|mail.yahoo.com')) & (email_df['Medium'] == 'referral'))]
    print(f"Email Channel Group - Filtered Rows: {len(email_df)}")
    email_df['new_channel_group'] = 'Email'
    cleaned_df = pd.concat([cleaned_df, email_df], ignore_index=True)

    # Filter for Display and Cross-network channel groups
    display_df = df[df['Default_Channel_Group'].isin(['Display', 'Cross-network'])]
    print(f"Display and Cross-network Channel Groups - Initial Rows: {len(display_df)}")

    # Apply additional filters for Source, Medium, and Campaign
    display_df = display_df[(display_df['Source'].isin(['google', 'bing'])) &
                            (display_df['Medium'] == 'cpc') &
                            (display_df['Campaign'].str.contains('display', case=False))]
    print(f"Display and Cross-network Channel Groups - Filtered Rows: {len(display_df)}")

    # Assign the new channel group
    display_df['new_channel_group'] = 'Paid Social + Display'

    # Concatenate the filtered data with the cleaned_df
    cleaned_df = pd.concat([cleaned_df, display_df], ignore_index=True)


    # Filter for Organic Search channel group
    organic_search_df = df[df['Default_Channel_Group'] == 'Organic Search']
    print(f"Organic Search Channel Group - Initial Rows: {len(organic_search_df)}")
    organic_search_df = organic_search_df[organic_search_df['Medium'] == 'organic']
    print(f"Organic Search Channel Group - Filtered Rows: {len(organic_search_df)}")
    organic_search_df['new_channel_group'] = 'Organic Search (SEO)'
    cleaned_df = pd.concat([cleaned_df, organic_search_df], ignore_index=True)

    # Filter for Organic Social channel group
    organic_social_df = df[df['Default_Channel_Group'] == 'Organic Social']
    print(f"Organic Social Channel Group - Initial Rows: {len(organic_social_df)}")
    organic_social_df = organic_social_df[(organic_social_df['Medium'].isin(['referral', 'instagram'])) &
                                          (organic_social_df['Source'].str.contains('facebook|reddit|snapchat|linkedin|instagram|messenger|linktr.ee|social', case=False))]
    print(f"Organic Social Channel Group - Filtered Rows: {len(organic_social_df)}")
    organic_social_df['new_channel_group'] = 'Organic Social'
    cleaned_df = pd.concat([cleaned_df, organic_social_df], ignore_index=True)

    # Filter for Organic Video channel group
    organic_video_df = df[df['Default_Channel_Group'] == 'Organic Video']
    print(f"Organic Video Channel Group - Initial Rows: {len(organic_video_df)}")
    organic_video_df = organic_video_df[(organic_video_df['Source'].str.contains('youtube.com', case=False)) &
                                        (organic_video_df['Medium'] == 'referral')]
    print(f"Organic Video Channel Group - Filtered Rows: {len(organic_video_df)}")
    organic_video_df['new_channel_group'] = 'Organic Video'
    cleaned_df = pd.concat([cleaned_df, organic_video_df], ignore_index=True)

    # Filter for Referral channel group
    referral_df = df[df['Default_Channel_Group'] == 'Referral']
    print(f"Referral Channel Group - Initial Rows: {len(referral_df)}")
    referral_df = referral_df[
        (referral_df['Medium'] == 'referral') & 
        (referral_df['Source'] != 'travelnursehousing.com') & 
        (~referral_df['Source'].str.contains('mail.google.com|facebook.com|instagram|linktr.ee|webmailb.netzero.net|mail.yahoo.com|linkedin.com|webmail1.earthlink.net|l.messenger.com|webmaila.juno.com|reddit.com|snapchat.com|youtube.com|outlook.live.com', case=False))
    ]
    print(f"Referral Channel Group - Filtered Rows: {len(referral_df)}")
    referral_df['new_channel_group'] = 'Referral'
    cleaned_df = pd.concat([cleaned_df, referral_df], ignore_index=True)
    
    # Filter for Referral channel group - Travelnursehousing.com
    referral_df_tnh = df[df['Default_Channel_Group'] == 'Referral']
    print(f"Referral Channel Group (TNH)- Initial Rows: {len(referral_df_tnh)}")
    referral_df_tnh = referral_df_tnh[(referral_df_tnh['Medium'] == 'referral') & (referral_df_tnh['Source'] == 'travelnursehousing.com')]
    print(f"Referral Channel Group (TNH)- Filtered Rows: {len(referral_df_tnh)}")
    referral_df_tnh['new_channel_group'] = 'SEM Non-Brand'
    cleaned_df = pd.concat([cleaned_df, referral_df_tnh], ignore_index=True)

    # Filter for Paid Social channel group
    paid_social_df = df[df['Default_Channel_Group'] == 'Paid Social']
    print(f"Paid Social Channel Group - Initial Rows: {len(paid_social_df)}")
    paid_social_df = paid_social_df[(paid_social_df['Source'].isin(['facebook', 'fb'])) &
                                    (paid_social_df['Medium'] == 'cpc')]
    print(f"Paid Social Channel Group - Filtered Rows: {len(paid_social_df)}")
    paid_social_df['new_channel_group'] = 'Paid Social + Display'
    cleaned_df = pd.concat([cleaned_df, paid_social_df], ignore_index=True)

    # Filter for Paid Search channel group - SEM Non-Brand
    paid_search_non_brand_df = df[df['Default_Channel_Group'] == 'Paid Search']
    print(f"Paid Search (SEM Non-Brand) Channel Group - Initial Rows: {len(paid_search_non_brand_df)}")
    paid_search_non_brand_df = paid_search_non_brand_df[(paid_search_non_brand_df['Source'].isin(['google', 'bing'])) &
                                                        (paid_search_non_brand_df['Medium'] == 'cpc') &
                                                        (~paid_search_non_brand_df['Campaign'].str.contains('display|organic|not set|prospecting|retargeting|awareness|brand|tenant|competitor|(referral)|(direct)', case=False))]
    print(f"Paid Search (SEM Non-Brand) Channel Group - Filtered Rows: {len(paid_search_non_brand_df)}")
    paid_search_non_brand_df['new_channel_group'] = 'SEM Non-Brand'
    cleaned_df = pd.concat([cleaned_df, paid_search_non_brand_df], ignore_index=True)

    # Filter for Paid Search channel group - SEM Brand
    paid_search_brand_df = df[df['Default_Channel_Group'] == 'Paid Search']
    print(f"Paid Search (SEM Brand) Channel Group - Initial Rows: {len(paid_search_brand_df)}")
    paid_search_brand_df = paid_search_brand_df[(paid_search_brand_df['Source'].isin(['google', 'bing'])) &
                                                (paid_search_brand_df['Medium'] == 'cpc') &
                                                (~paid_search_brand_df['Campaign'].str.contains('display|organic|not set|prospecting|retargeting|awareness', case=False)) &
                                                (paid_search_brand_df['Campaign'].str.contains('brand', case=False))]
    print(f"Paid Search (SEM Brand) Channel Group - Filtered Rows: {len(paid_search_brand_df)}")
    paid_search_brand_df['new_channel_group'] = 'SEM Brand'
    cleaned_df = pd.concat([cleaned_df, paid_search_brand_df], ignore_index=True)

    # Filter for Paid Search channel group - SEM Non-Brand Tenant
    paid_search_non_brand_tenant_df = df[df['Default_Channel_Group'] == 'Paid Search']
    print(f"Paid Search (SEM Non-Brand Tenant) Channel Group - Initial Rows: {len(paid_search_non_brand_tenant_df)}")
    paid_search_non_brand_tenant_df = paid_search_non_brand_tenant_df[(paid_search_non_brand_tenant_df['Source'].isin(['google', 'bing'])) &
                                                                      (paid_search_non_brand_tenant_df['Medium'] == 'cpc') &
                                                                      (~paid_search_non_brand_tenant_df['Campaign'].str.contains('display|organic|not set|prospecting|retargeting|awareness|brand', case=False)) &
                                                                      (paid_search_non_brand_tenant_df['Campaign'].str.contains('tenant|competitor', case=False))]
    print(f"Paid Search (SEM Non-Brand Tenant) Channel Group - Filtered Rows: {len(paid_search_non_brand_tenant_df)}")
    paid_search_non_brand_tenant_df['new_channel_group'] = 'SEM Non-Brand - Tenant'
    cleaned_df = pd.concat([cleaned_df, paid_search_non_brand_tenant_df], ignore_index=True)

    return cleaned_df

# Apply deduplication to the Display and Cross-network channel groups
deduplicated_display_cross_df = deduplicate_display_and_cross_network(df)

# Filter the remaining rows that are not in 'Display' or 'Cross-network'
remaining_df = df[~df['Default_Channel_Group'].isin(['Display', 'Cross-network'])]

# Combine the deduplicated rows with the remaining data
cleaned_df = pd.concat([remaining_df, deduplicated_display_cross_df], ignore_index=True)

# Ensure the new DataFrame is consistent
print(f"Final Cleaned DataFrame Rows: {len(cleaned_df)}")

# Apply the function to assign new channel group
full_cleaned_df = assign_new_channel_group(cleaned_df)

# Save the cleaned DataFrame to a new table in the database
full_cleaned_df.to_sql('ga4_channel_groups_cleaned', conn, if_exists='replace', index=False)

# Save the cleaned DataFrame to a CSV file for review
full_cleaned_df.to_csv('ga4-reports/output/ga4_channel_groups_cleaned.csv', index=False)

# Close the database connection
conn.close()

print("The table ga4_channel_groups_cleaned has been created and saved to a CSV file.")
