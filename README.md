# Hello Lambda — Docker & ECR hands-on

A minimal Python AWS Lambda packaged as a container image. It exists to teach the container-image Lambda workflow end-to-end: build, scan, push to ECR, deploy, test locally with RIE, and ship through GitHub Actions.

This directory pairs with the [Docker & ECR for Lambda](../docker-ecr-lambda-deck/slides.md) enablement deck.

## What's in here

| File | Purpose |
|---|---|
| `Dockerfile` | Minimal Lambda container image built on `public.ecr.aws/lambda/python:3.12` |
| `requirements.txt` | Single third-party dependency (`requests`) so `pip install` does real work |
| `app.py` | Handler that fetches the public IP of the execution environment |
| `deploy.yml` | GitHub Actions workflow — build, scan, push to ECR, deploy to Lambda |
| `README.md` | This file |

## Prerequisites

- Docker (or Docker Desktop) running locally
- AWS CLI v2 configured with credentials that can push to ECR and update Lambda
- `jq` for pretty-printing responses (optional)
- An AWS account and a region to work in (examples use `eu-west-1`)

Set these environment variables once to avoid repeating them:

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=eu-west-1
export ECR_REPO=hello-lambda
export IMAGE_TAG=v1.0.0
export FUNCTION_NAME=hello-lambda
export REGISTRY=${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com
```

## 1. Build the image

From inside `lambda-hands-on/`:

```bash
docker build -t ${ECR_REPO}:${IMAGE_TAG} .
```

Verify:

```bash
docker images ${ECR_REPO}
```

### Why the Dockerfile looks the way it does

```dockerfile
FROM public.ecr.aws/lambda/python:3.12

# Copy requirements and install dependencies
COPY requirements.txt ${LAMBDA_TASK_ROOT}
RUN pip install --no-cache-dir -r requirements.txt

# Copy function code
COPY app.py ${LAMBDA_TASK_ROOT}

# Set the handler (file.function)
CMD ["app.handler"]
```

- **`public.ecr.aws/lambda/python:3.12`** — the AWS-maintained Lambda base image. Ships with the Python runtime and the Runtime Interface Emulator (RIE), so the same image runs identically in Lambda and on your laptop.
- **`${LAMBDA_TASK_ROOT}`** — environment variable pointing to `/var/task`, the directory Lambda expects code in. Using the variable keeps the Dockerfile portable if AWS changes the path.
- **Copy order** — `requirements.txt` and `pip install` come before `COPY app.py`. Editing the handler only invalidates the last layer, so rebuilds stay fast.
- **`--no-cache-dir`** — stops `pip` from caching wheels inside the image. Keeps the image smaller.
- **`CMD ["app.handler"]`** — Lambda's `file.function` handler format. `app` is the filename (without `.py`), `handler` is the function.

## 2. Scan the image with Docker Scout

Scanning early is cheaper than scanning late. Run Scout before pushing.

```bash
# Quick overview
docker scout quickview ${ECR_REPO}:${IMAGE_TAG}

# Full CVE list with severity
docker scout cves ${ECR_REPO}:${IMAGE_TAG}

# Actionable fix recommendations
docker scout recommendations ${ECR_REPO}:${IMAGE_TAG}
```

### What to look for

- **Critical and high severity first.** Medium and low are worth tracking but rarely block a ship.
- **Most CVEs come from the base image, not your code.** The fix is usually "bump the base tag" rather than "rewrite the app".
- **`recommendations`** suggests newer base image tags and package versions that close known CVEs.

In a CI context, fail the build when critical or high CVEs appear:

```bash
docker scout cves ${ECR_REPO}:${IMAGE_TAG} \
  --only-severity critical,high \
  --exit-code
```

Requires a free Docker Hub account and `docker login`.

## 3. Push to Amazon ECR

### Create the repository (one-time)

```bash
aws ecr create-repository \
  --repository-name ${ECR_REPO} \
  --region ${AWS_REGION} \
  --image-scanning-configuration scanOnPush=true \
  --image-tag-mutability IMMUTABLE
```

Two flags worth calling out:

- **`scanOnPush=true`** — ECR runs its own vulnerability scan every time you push. Defence in depth alongside Scout.
- **`IMMUTABLE` tags** — prevents overwriting an existing tag. A deployed tag cannot silently change under you.

### Authenticate Docker to ECR

```bash
aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS --password-stdin ${REGISTRY}
```

The password is piped straight to `docker login` and never written to disk.

### Tag and push

```bash
docker tag ${ECR_REPO}:${IMAGE_TAG} ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}
docker push ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}
```

### Set a lifecycle policy (recommended)

Auto-delete untagged images after seven days so ECR doesn't fill up with orphan layers:

```bash
cat > lifecycle.json <<'EOF'
{
  "rules": [
    {
      "rulePriority": 1,
      "description": "Expire untagged images after 7 days",
      "selection": {
        "tagStatus": "untagged",
        "countType": "sinceImagePushed",
        "countUnit": "days",
        "countNumber": 7
      },
      "action": { "type": "expire" }
    }
  ]
}
EOF

aws ecr put-lifecycle-policy \
  --repository-name ${ECR_REPO} \
  --lifecycle-policy-text file://lifecycle.json
```

## 4. Create the Lambda function

### IAM role (one-time)

```bash
cat > trust-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "lambda.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role \
  --role-name ${FUNCTION_NAME}-role \
  --assume-role-policy-document file://trust-policy.json

aws iam attach-role-policy \
  --role-name ${FUNCTION_NAME}-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
```

### Create the function

```bash
aws lambda create-function \
  --function-name ${FUNCTION_NAME} \
  --package-type Image \
  --code ImageUri=${REGISTRY}/${ECR_REPO}:${IMAGE_TAG} \
  --role arn:aws:iam::${AWS_ACCOUNT_ID}:role/${FUNCTION_NAME}-role \
  --timeout 30 \
  --memory-size 512 \
  --region ${AWS_REGION}
```

`--package-type Image` tells Lambda to pull from ECR. The image URI must be in the same region as the function.

### Invoke it

```bash
aws lambda invoke \
  --function-name ${FUNCTION_NAME} \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  response.json

cat response.json
```

Expected output:

```json
{"statusCode": 200, "ip": "52.x.y.z"}
```

### Update the image (subsequent deploys)

```bash
# Build a new tag
export IMAGE_TAG=v1.0.1
docker build -t ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG} .
docker push ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}

# Point Lambda at the new image
aws lambda update-function-code \
  --function-name ${FUNCTION_NAME} \
  --image-uri ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}

# Wait for the update to finish before invoking again
aws lambda wait function-updated --function-name ${FUNCTION_NAME}
```

## 5. Test locally with the Runtime Interface Emulator

RIE is built into every AWS Lambda base image. No extra setup.

```bash
docker run --rm -p 9000:8080 ${ECR_REPO}:${IMAGE_TAG}
```

In another terminal:

```bash
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d '{}'
```

The response matches what Lambda returns in the cloud:

```json
{"statusCode": 200, "ip": "..."}
```

### Why this matters

- **Same image, same result.** No drift between local and production.
- **Iterate fast.** Edit `app.py`, rebuild, re-invoke. No upload, no wait.
- **Debug with familiar tools.** `print` goes to your terminal. Stacks traces show up instantly.
- **Test event shapes.** Pass any JSON payload to `-d` to simulate SQS messages, API Gateway events, EventBridge rules.

### Passing a realistic event

```bash
curl -XPOST "http://localhost:9000/2015-03-31/functions/function/invocations" \
  -d '{"Records":[{"body":"hello"}]}'
```

## 6. CI/CD with GitHub Actions

The workflow in [`deploy.yml`](./deploy.yml) automates everything above. It triggers on a published GitHub Release (or manual dispatch) and does:

1. Build the image with Docker Buildx, with GHA-backed cache
2. Tag it with the commit SHA (immutable, traceable)
3. Scan it with Docker Scout, failing on critical or high CVEs
4. Push to ECR
5. Update the Lambda function to the new image
6. Wait for the deploy to propagate

### Best practices applied

Every one of these maps to a recommendation from the review of the customer's original `deploy.yml`:

| Practice | How it's implemented |
|---|---|
| **OIDC federation, no long-lived keys** | `permissions: id-token: write` + `aws-actions/configure-aws-credentials@v4` with `role-to-assume` |
| **Two IAM roles, least privilege** | `AWS_ROLE_TO_ASSUME_ECR` for push, `AWS_ROLE_TO_ASSUME_LAMBDAS` for deploy |
| **Immutable, traceable tags** | Image tagged with `${{ github.sha }}`; deploy references the SHA, not `:latest` |
| **Split build and deploy jobs** | `build` emits the image URI; `deploy` consumes it via `needs:` |
| **Matrix for Lambda deploys** | `strategy.matrix.function` — collapses repeated steps; easy to add functions |
| **Scout scan before push** | `docker/scout-action@v1` with `exit-code: true` fails the build on critical or high CVEs |
| **Official AWS CLI for deploy** | `aws lambda update-function-code` instead of a third-party action |
| **Build cache** | `cache-from: type=gha` / `cache-to: type=gha,mode=max` |
| **Rollback via `workflow_dispatch`** | Optional `image_tag` input lets ops pin a specific prior SHA |
| **Wait for update completion** | `aws lambda wait function-updated` before the job ends |
| **Current action major versions** | `docker/metadata-action@v5`, `docker/build-push-action@v6` |
| **`mask-aws-account-id` default** | Keeps the account ID out of workflow logs |

### Required GitHub configuration

Under repository **Settings → Secrets and variables → Actions → Variables**:

| Variable | Example |
|---|---|
| `AWS_ROLE_TO_ASSUME_ECR` | `arn:aws:iam::<account>:role/github-ecr-push` |
| `AWS_ROLE_TO_ASSUME_LAMBDAS` | `arn:aws:iam::<account>:role/github-lambda-deploy` |
| `ECR_REPO` | `hello-lambda` |

Both IAM roles need a trust policy that trusts GitHub's OIDC provider, scoped to your specific repository and branch. See the [GitHub + AWS OIDC guide](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services) for the trust policy template.

### Triggering a deploy

- **On release:** create a GitHub Release. The workflow runs automatically and deploys the tagged commit.
- **Manual:** use the **Run workflow** button on the Actions tab. Leave `image_tag` empty to deploy the current SHA, or supply a prior SHA to roll back.

### What this workflow intentionally skips

Kept out to stay teachable. In production you'd want:

- A GitHub Environment with required reviewers gating `deploy`.
- Lambda aliases and versions instead of overwriting `$LATEST`.
- A post-deploy smoke test that invokes the function and asserts the response.

## Cleanup

When you're done experimenting:

```bash
# Delete the Lambda function
aws lambda delete-function --function-name ${FUNCTION_NAME}

# Delete the IAM role
aws iam detach-role-policy \
  --role-name ${FUNCTION_NAME}-role \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole
aws iam delete-role --role-name ${FUNCTION_NAME}-role

# Delete all images and the ECR repository
aws ecr delete-repository \
  --repository-name ${ECR_REPO} \
  --region ${AWS_REGION} \
  --force
```

## References

- [Lambda — container image deployment](https://docs.aws.amazon.com/lambda/latest/dg/python-image.html)
- [ECR — user guide](https://docs.aws.amazon.com/AmazonECR/latest/userguide/)
- [Docker Scout](https://docs.docker.com/scout/)
- [AWS Lambda Runtime Interface Emulator](https://github.com/aws/aws-lambda-runtime-interface-emulator)
- [GitHub Actions — OIDC with AWS](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services)
- [Workshop: AWS Modernization with Docker](https://docker.awsworkshop.io)
