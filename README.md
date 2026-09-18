# How to build specification-driven data workflows with AWS Step Functions

Sample code for the AWS Compute Blog post *How to build specification-driven data workflows with AWS Step Functions* (link to be added when the post is published). The design rationale behind the pattern is described in the AWS Architecture Blog post [Specification-driven composition for flexible data workflows](https://aws.amazon.com/blogs/architecture/specification-driven-composition-for-flexible-data-workflows/).

## What this sample shows

- Define data workflows as JSON specifications instead of scripts.
- Register reusable, versioned transformation functions (AWS Lambda) in a capability registry (Amazon DynamoDB).
- Validate a specification, then generate, publish, and run an AWS Step Functions workflow from it at runtime.

You change pipeline behavior by updating the specification, not the code.

> **Note:** This is sample code, for non-production usage. You should work with your security and legal teams to meet your organizational security, regulatory and compliance requirements before deployment. The sample uses simple defaults (for example, no bucket access logging or versioning and no dead-letter queues). The registry table is encrypted with a customer-managed AWS KMS key with automatic rotation, and point-in-time recovery is enabled. The Lambda functions call AWS service endpoints over TLS and are not placed in a VPC. Review and harden the IAM policies, logging, and encryption settings before using the pattern in a production environment. See the [AWS Well-Architected Serverless Applications Lens](https://docs.aws.amazon.com/wellarchitected/latest/serverless-applications-lens/welcome.html) for guidance.

## Architecture

The solution has three layers:

1. **Specification layer.** You upload a workflow specification and input data to an Amazon S3 bucket.
2. **Composition layer.** A Lambda-based composer reads and validates the specification, resolves the pinned capability versions in a DynamoDB table, builds a Step Functions definition in Amazon States Language, publishes it as an immutable state machine version, and starts an execution under a unique run identifier. Validation happens before any workflow is assembled or started, so specification errors surface at composition time rather than mid-run.
3. **Processing layer.** Step Functions invokes the `format_date` and `normalize_currency` Lambda functions in sequence. Each step writes its output to Amazon S3, which becomes the input for the next step. Logs and metrics are captured in Amazon CloudWatch.

Each run writes its artifacts under a unique `runs/<run_id>/` prefix, so runs never overwrite each other. The final artifact is named after the target dataset declared in the specification. The Step Functions execution name equals the run identifier, which links the console view to the S3 artifacts.

## Repository structure

| Path | Purpose |
| --- | --- |
| `input/raw_orders.json` | Sample order data with inconsistent date and currency formats |
| `specs/orders-spec.json` | Workflow specification: field mappings and pinned capability versions |
| `composer/composer.py` | Composer Lambda function: validates the specification, resolves capabilities, builds and runs the workflow |
| `format_date/format_date.py` | Transformation capability: standardizes dates to ISO 8601 |
| `normalize_currency/normalize_currency.py` | Transformation capability: converts currency strings to numeric values |
| `events/composer-event.json` | Sample invocation event for the composer |
| `template.yaml` | AWS SAM template for the bucket, KMS key, table, IAM roles, and Lambda functions |

## Prerequisites

- An AWS account with access to AWS Lambda, AWS Step Functions, Amazon S3, Amazon DynamoDB, Amazon CloudWatch, AWS Key Management Service (AWS KMS), and AWS Identity and Access Management (IAM).
- [AWS Command Line Interface (AWS CLI)](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) and [AWS Serverless Application Model (AWS SAM) CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html) installed.
- Python 3.12.
- `jq` (optional, used to read fields from the composer response).

## Cost

Running this walkthrough typically costs less than $1. The customer-managed AWS KMS key is billed at about $1 per month, prorated hourly, until you delete the stack. Follow the clean-up steps when you are done.

## Deploy and run

### 1. Deploy the infrastructure

```bash
sam build
sam deploy --guided
```

### 2. Retrieve the stack outputs

```bash
STACK_NAME=<your-stack-name>
BUCKET=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ArtifactsBucketName'].OutputValue" \
  --output text)
TABLE=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='RegistryTableName'].OutputValue" \
  --output text)
FORMAT_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='FormatDateFunctionArn'].OutputValue" \
  --output text)
NORMALIZE_ARN=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='NormalizeCurrencyFunctionArn'].OutputValue" \
  --output text)
COMPOSER=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --query "Stacks[0].Outputs[?OutputKey=='ComposerFunctionName'].OutputValue" \
  --output text)
```

### 3. Register capabilities

Each registry entry maps a logical capability name and version to a Lambda function. Specifications pin an explicit capability version, so a workflow run is reproducible even after newer versions are registered.

```bash
aws dynamodb put-item \
  --table-name "$TABLE" \
  --item '{"capability_name":{"S":"format_date"},"version":{"S":"1.0"},"lambda_arn":{"S":"'"$FORMAT_ARN"'"},"description":{"S":"Standardize date values to ISO 8601 (YYYY-MM-DD)"}}'
aws dynamodb put-item \
  --table-name "$TABLE" \
  --item '{"capability_name":{"S":"normalize_currency"},"version":{"S":"1.0"},"lambda_arn":{"S":"'"$NORMALIZE_ARN"'"},"description":{"S":"Convert currency strings to numeric values"}}'
```

### 4. Upload the input data and specification

```bash
aws s3 cp input/raw_orders.json "s3://$BUCKET/input/raw_orders.json"
aws s3 cp specs/orders-spec.json "s3://$BUCKET/specs/orders-spec.json"
```

### 5. Prepare the invocation event

`events/composer-event.json` uses the placeholder bucket name `amzn-s3-demo-spec-workflows-250831`. Replace it with your bucket name:

```bash
sed "s/amzn-s3-demo-spec-workflows-250831/$BUCKET/g" events/composer-event.json > /tmp/composer-event.json
```

### 6. Run the workflow

```bash
aws lambda invoke \
  --function-name "$COMPOSER" \
  --payload fileb:///tmp/composer-event.json \
  /tmp/composer-response.json
cat /tmp/composer-response.json
```

The response includes `run_id`, `state_machine_arn`, `state_machine_version_arn`, `execution_arn`, and `final_output_s3_uri`.

### 7. Inspect the results

```bash
aws s3 cp "$(jq -r .final_output_s3_uri /tmp/composer-response.json)" -
```

Example output:

```json
[
  {
    "order_id": "A1001",
    "order_date": "2026-03-01",
    "amount": "100.50",
    "currency": "USD",
    "amount_normalized": 100.5
  },
  {
    "order_id": "A1002",
    "order_date": "2026-03-02",
    "amount": "250,00",
    "currency": "EUR",
    "amount_normalized": 250.0
  }
]
```

You can also view the execution graph in the AWS Step Functions console and inspect logs in Amazon CloudWatch.

### 8. See validation in action

Edit `specs/orders-spec.json` and change a pinned version to one that is not registered (for example, `"version": "9.9"`), or delete a `source_field` line. Re-upload the specification and invoke the composer again:

```bash
aws s3 cp specs/orders-spec.json "s3://$BUCKET/specs/orders-spec.json"
aws lambda invoke \
  --function-name "$COMPOSER" \
  --payload fileb:///tmp/composer-event.json \
  /tmp/composer-response.json
cat /tmp/composer-response.json
```

The composer rejects the specification before any state machine is created or started, for example with `Capability not found: format_date v9.9` or `Mapping 1: missing required field: source_field`. Revert the specification and re-upload it before continuing.

## Clean up

Delete the Step Functions state machine. This also removes its published versions.

```bash
aws stepfunctions list-state-machines
aws stepfunctions delete-state-machine --state-machine-arn <STATE_MACHINE_ARN>
```

Empty the S3 bucket and delete the stack:

```bash
aws s3 rm s3://$BUCKET --recursive
sam delete
```

Deleting the stack schedules the KMS key for deletion. The key is removed after seven days.

## Security considerations

The sample relies on IAM for authentication and authorization. Control who can start runs by granting `lambda:InvokeFunction` on the composer, and write access to the `specs/` prefix, only to the principals that own pipeline changes. Restrict write access to the registry table to the role that releases capabilities, because a registry entry decides which function a capability resolves to. AWS CloudTrail records the control-plane calls the composer makes, including CreateStateMachine, UpdateStateMachine, and PublishStateMachineVersion, so each published version can be traced to its caller.

For production or regulated use, also enable S3 versioning and access logging on the artifacts bucket, consider S3 Object Lock for specifications, track registry changes with DynamoDB Streams or CloudTrail data events, and place the functions in a VPC with interface endpoints if your network policy requires it.

## Related resources

- [Specification-driven composition for flexible data workflows](https://aws.amazon.com/blogs/architecture/specification-driven-composition-for-flexible-data-workflows/) (AWS Architecture Blog)
- [AWS Step Functions Developer Guide](https://docs.aws.amazon.com/step-functions/latest/dg/welcome.html)
- [State machine versions in Step Functions](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-state-machine-version.html)
- [Amazon States Language](https://docs.aws.amazon.com/step-functions/latest/dg/concepts-amazon-states-language.html)

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
