import requests


def handler(event, context):
    """Lambda handler that returns the public IP of the execution environment."""
    response = requests.get("https://checkip.amazonaws.com", timeout=5)
    return {
        "statusCode": 200,
        "ip": response.text.strip(),
    }
