
from service import retrieveStoicQuote, retrieveNotionDatabase
import utils
from send_email import send_email
import html_email as html
import datetime
from datetime import timezone

#Variables
token = "secret_3NgfVbobBT1DXSXZvr0E87ZS4PDRDTNDodwxK7OuOI8"
database_Id = "5ce18cbb7a354ebfa430aec3f574067a"
email_password = "ybhfzgsjyaujnors"
email_from = "ian@primedesign.co"
email_to = "ian@primedesign.co"

# Notion api config
password = "Bearer " + token 
headers = {
    "Authorization": password,
    "Notion-version": "2022-06-28"
}

tomorrow = (
    datetime.datetime.now(timezone.utc) +
    datetime.timedelta(days=1)
).astimezone().isoformat()

# Notion api database block http request
database = retrieveNotionDatabase.retrieveDatabase(
    databaseId=database_Id,
    headers=headers,
    save_to_json=False,
)

# Print retrieve database data
# utils.debugDatabaseObject(database)

# Get data we want from database.json object
database_list = utils.decodeDatabase(database)
dbProperties = utils.databaseProperties(database_list)

# Filter columns of the database
dbProperties = ['Title', 'Text']
# Data to html table
title = "\n".join(html.html_table_column(dbProperties))
rows = "\n".join(html.html_table_row(
    database_list,
    dbProperties
))
table_html = html.construct_html_table(title, rows)

# Get random stoic quote
author, stoic_quote = retrieveStoicQuote.random_stoic_quote()

html_msg = html.construct_html_msg(
    table_html,
    html.style,
    html.quote_html(author, stoic_quote)  # Format stoic quote to html
)

# utils.save_html(html_msg)

# Send email with html msg
send_email(
    html_msg,
    email_from,
    email_to,
    email_password
)
