import json
from datetime import datetime
from urllib.parse import urlparse

import boto3

s3 = boto3.client("s3")


# Apply the date transformation to one field and write the result to S3.
def lambda_handler(event, context):
    rows = load_rows(event["current_s3_uri"])
    mapping = event["mapping"]

    source = mapping["source_field"]
    target = mapping["target_field"]

    for row in rows:
        row[target] = normalize_date(row[source])

    save_rows(rows, event["output_s3_uri"])
    return {"output_s3_uri": event["output_s3_uri"]}


# Convert supported date formats to ISO 8601, leaving unknown formats unchanged.
def normalize_date(value):
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return value


# Read a JSON array of rows from S3.
def load_rows(s3_uri):
    parsed = urlparse(s3_uri)
    obj = s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
    rows = json.loads(obj["Body"].read().decode("utf-8"))
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Input must be a JSON array of objects")
    return rows


# Write a JSON array of rows to S3.
def save_rows(rows, s3_uri):
    parsed = urlparse(s3_uri)
    s3.put_object(
        Bucket=parsed.netloc,
        Key=parsed.path.lstrip("/"),
        Body=json.dumps(rows)
    )