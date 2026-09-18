import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
from uuid import uuid4

import boto3

s3 = boto3.client("s3")
dynamodb = boto3.resource("dynamodb")
sfn = boto3.client("stepfunctions")

REGISTRY_TABLE = os.environ["REGISTRY_TABLE"]
OUTPUT_BUCKET = os.environ["OUTPUT_BUCKET"]
STATE_MACHINE_ROLE_ARN = os.environ["STATE_MACHINE_ROLE_ARN"]

STATE_MACHINE_NAME_PATTERN = re.compile(r"^orders-[A-Za-z0-9-]{1,72}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VERSION_PATTERN = re.compile(r"^[0-9]+(\.[0-9]+)*$")


# Validate the specification, compose the workflow, and start one run.
def lambda_handler(event, context):
    try:
        state_machine_name = event.get("state_machine_name", "orders-transform")
        if not STATE_MACHINE_NAME_PATTERN.match(state_machine_name):
            raise ValueError(f"Invalid state_machine_name: {state_machine_name}")

        spec = load_json_from_s3(event["spec_s3_uri"])
        validate_spec(spec)

        capabilities = {}
        for mapping in spec["mappings"]:
            transformation = mapping["transformation"]
            key = (transformation["capability"], transformation["version"])
            capabilities[key] = get_capability(*key)

        run_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid4().hex[:8]
        )
        definition = build_definition(spec, capabilities, run_id)

        state_machine_arn, version_arn = upsert_state_machine(
            state_machine_name, definition, context
        )

        execution_input = {
            "current_s3_uri": event["input_s3_uri"]
        }

        response = sfn.start_execution(
            stateMachineArn=version_arn,
            name=run_id,
            input=json.dumps(execution_input)
        )

        return {
            "run_id": run_id,
            "state_machine_arn": state_machine_arn,
            "state_machine_version_arn": version_arn,
            "execution_arn": response["executionArn"],
            "final_output_s3_uri": final_output_s3_uri(spec, run_id)
        }
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        raise


# Reject a malformed specification before anything is assembled.
def validate_spec(spec):
    for section in ("source", "target", "mappings"):
        if section not in spec:
            raise ValueError(f"Specification missing required section: {section}")
    for section in ("source", "target"):
        dataset = spec[section].get("dataset")
        if not isinstance(dataset, str) or not IDENTIFIER_PATTERN.match(dataset):
            raise ValueError(f"Specification {section}.dataset is missing or invalid")
    if not isinstance(spec["mappings"], list) or not spec["mappings"]:
        raise ValueError("Specification must define at least one mapping")
    for i, mapping in enumerate(spec["mappings"], start=1):
        for field in ("source_field", "target_field", "transformation"):
            if field not in mapping:
                raise ValueError(f"Mapping {i}: missing required field: {field}")
        for field in ("source_field", "target_field"):
            if not IDENTIFIER_PATTERN.match(str(mapping[field])):
                raise ValueError(f"Mapping {i}: invalid {field}")
        for field in ("capability", "version"):
            if field not in mapping["transformation"]:
                raise ValueError(
                    f"Mapping {i}: transformation missing required field: {field}"
                )
        if not IDENTIFIER_PATTERN.match(str(mapping["transformation"]["capability"])):
            raise ValueError(f"Mapping {i}: invalid capability name")
        if not VERSION_PATTERN.match(str(mapping["transformation"]["version"])):
            raise ValueError(f"Mapping {i}: invalid capability version")


# Read a JSON object from the artifacts bucket only.
def load_json_from_s3(s3_uri):
    parsed = urlparse(s3_uri)
    if parsed.scheme != "s3" or parsed.netloc != OUTPUT_BUCKET:
        raise ValueError(f"S3 URI must point to bucket {OUTPUT_BUCKET}: {s3_uri}")
    obj = s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
    return json.loads(obj["Body"].read().decode("utf-8"))


# Resolve a pinned capability version from the registry.
def get_capability(name, version):
    table = dynamodb.Table(REGISTRY_TABLE)
    response = table.get_item(Key={"capability_name": name, "version": version})
    item = response.get("Item")
    if not item:
        raise ValueError(f"Capability not found: {name} v{version}")
    return item


# Build the S3 URI of the final artifact, named after the target dataset.
def final_output_s3_uri(spec, run_id):
    return f"s3://{OUTPUT_BUCKET}/runs/{run_id}/{spec['target']['dataset']}.json"


# Generate the Amazon States Language definition, one Task state per mapping.
def build_definition(spec, capabilities, run_id):
    mappings = spec["mappings"]
    states = {}

    for i, mapping in enumerate(mappings, start=1):
        transformation = mapping["transformation"]
        capability_name = transformation["capability"]
        capability = capabilities[(capability_name, transformation["version"])]

        state_name = f"Step{i}_{capability_name}"
        if i < len(mappings):
            output_s3_uri = f"s3://{OUTPUT_BUCKET}/runs/{run_id}/step{i}_{capability_name}.json"
        else:
            output_s3_uri = final_output_s3_uri(spec, run_id)

        states[state_name] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::lambda:invoke",
            "Parameters": {
                "FunctionName": capability["lambda_arn"],
                "Payload": {
                    "current_s3_uri.$": "$.current_s3_uri",
                    "output_s3_uri": output_s3_uri,
                    "mapping": mapping
                }
            },
            "ResultSelector": {
                "current_s3_uri.$": "$.Payload.output_s3_uri"
            },
            "ResultPath": "$"
        }

        if i < len(mappings):
            next_name = f"Step{i+1}_{mappings[i]['transformation']['capability']}"
            states[state_name]["Next"] = next_name
        else:
            states[state_name]["End"] = True

    return json.dumps({
        "StartAt": list(states.keys())[0],
        "States": states
    })


# Create or update the state machine at its deterministic ARN and publish an immutable version.
def upsert_state_machine(name, definition, context):
    arn_parts = context.invoked_function_arn.split(":")
    partition, region, account_id = arn_parts[1], arn_parts[3], arn_parts[4]
    state_machine_arn = f"arn:{partition}:states:{region}:{account_id}:stateMachine:{name}"
    try:
        response = sfn.update_state_machine(
            stateMachineArn=state_machine_arn,
            definition=definition,
            roleArn=STATE_MACHINE_ROLE_ARN,
            publish=True
        )
        return state_machine_arn, response["stateMachineVersionArn"]
    except sfn.exceptions.StateMachineDoesNotExist:
        response = sfn.create_state_machine(
            name=name,
            definition=definition,
            roleArn=STATE_MACHINE_ROLE_ARN,
            type="STANDARD",
            publish=True
        )
        return response["stateMachineArn"], response["stateMachineVersionArn"]
