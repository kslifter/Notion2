import requests
import json

HeadersType = dict[str, str]

def retrieveDatabase(
        databaseId: str,
        headers: HeadersType,
        query: dict = {},
        save_to_json: bool = False) -> dict:

    # Construct database URL
    base_url = "https://api.notion.com/v1/databases/"
    url = base_url + databaseId + "/query"

    # POST
    try:
        response = requests.post(url=url, headers=headers, json=query)

        # Check for success
        if response.status_code == 200:
            database = response.json()

            if save_to_json:
                with open("./dailyEmail/saved-data/database.json", "w", encoding="utf8") as f:
                    json.dump(database, f, ensure_ascii=False)

            return database
        else:
            # Log error details
            print(f"Error {response.status_code} with query: {query}")
            print(f"Response Content: {response.text}")
            return dict()
    except requests.exceptions.RequestException as e:
        # Handle and log exceptions
        print(f"An error occurred while making the API request: {e}")
        return dict()
