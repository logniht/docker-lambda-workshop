# Hello Lambda — Docker & ECR hands-on

A minimal Python AWS Lambda packaged as a container image. It exists to teach the container-image Lambda workflow end-to-end: build, scan, push to ECR, deploy, test locally with RIE, and ship through GitHub Actions.

This directory pairs with the [Docker & ECR for Lambda](../docker-ecr-lambda-deck/slides.md) enablement deck.

## What's in here

| File | Purpose |
|---|---|
| `Dockerfile` | Minimal Lambda container image built on `public.ecr.aws/lambda/python:3.12` |
| `requirements.txt` | Single third-party dependency (`requests`) so `pip install` does real work |
| `app.py` | Handler that fetches the public IP of the execution environment |
| `trust-policy.json` | Lambda execution role trust policy (used in section 4) |
| `lifecycle.json` | ECR lifecycle policy that expires untagged images after 7 days (used in section 3) |
| `infra/github-oidc-roles.yaml` | CloudFormation template for the GitHub OIDC provider and IAM deploy roles |
| `infra/lambda-codepipeline.yaml` | CloudFormation template for the AWS CodePipeline alternative (section 7) |
| `infra/buildspec.yml` | CodeBuild buildspec used by the CodePipeline alternative |
| `README.md` | This file |

The GitHub Actions workflow lives at [`.github/workflows/deploy.yml`](./.github/workflows/deploy.yml) at the root of this repository — GitHub only discovers workflows at that path.

## Prerequisites

**For sections 1–5** (build, scan, push, deploy, local testing):

- Docker (or Docker Desktop) running locally
- AWS CLI v2 configured with credentials that can push to ECR and update Lambda
- `jq` for pretty-printing responses (optional)
- An AWS account and a region to work in (examples use `us-west-2`)

**Additionally for section 6** (CI/CD with GitHub Actions):

- A GitHub account (personal or organisation). The workshop repo is forked as part of the setup below — keep the fork public so you get free arm64 runners and free Actions minutes. Private forks work but arm64 runners may incur additional billing.
- Git installed locally
- GitHub CLI ([`gh`](https://cli.github.com/)) — optional, lets you fork-and-clone in one command
- Permission in the AWS account to create IAM roles and an OIDC identity provider

## Get the workshop repo

All hands-on commands run from inside your fork of the workshop repo.

**With the GitHub CLI (fastest — fork and clone in one step):**

```bash
# Check if already authenticated
gh auth status || gh auth login

gh repo fork logniht/docker-lambda-workshop --clone=true
cd docker-lambda-workshop
```

**Or in the browser (if `gh` isn't installed):**

1. Open the workshop repo on GitHub
2. Click **Fork** (top right) → **Create fork**
3. Clone your fork locally:

```bash
git clone https://github.com/<your-username>/docker-lambda-workshop.git
cd docker-lambda-workshop
```

All paths in the rest of this README are relative to the repo root.

## Set environment variables

Set these once to avoid repeating them:

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-west-2
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

Check which CPU architecture the image was built for — it should match the Lambda function's `--architectures` setting:

```bash
docker image inspect ${ECR_REPO}:${IMAGE_TAG} --format '{{.Architecture}}'
```

On Apple Silicon Macs this prints `arm64`. On Intel/AMD machines it prints `amd64`. The Lambda function created in section 4 is configured for `arm64`, so Apple Silicon users can build native without any `--platform` flag. Intel/AMD users should either build with `--platform linux/arm64` (requires emulation via Docker Desktop Rosetta or QEMU) or switch the Lambda to `--architectures x86_64` in section 4.

### Why arm64?

- **Native on Apple Silicon.** No QEMU/Rosetta emulation during build.
- **~20% cheaper on Lambda** — Graviton pricing.
- **AWS's default going forward.** New AWS services and base images target arm64 first.

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

### Install the Scout CLI plugin (one-time)

Scout ships bundled with recent Docker Desktop, but on Linux or older Docker installations you may see:

```
docker: 'scout' is not a docker command
```

Install it with the official one-liner (macOS and Linux):

```bash
curl -fsSL https://raw.githubusercontent.com/docker/scout-cli/main/install.sh | sh -s --
```

Verify:

```bash
docker scout version
```

Scout needs a free Docker Hub account to pull CVE data. Log in once:

```bash
docker login
```

### Run the scan

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

> **Immutable tags and `:latest` don't mix.** With `IMMUTABLE`, pushing any tag that already exists — including `:latest` — fails with `tag immutable`. This is the feature working, not a bug. Use unique tags per build (commit SHA or semver) and skip `:latest` entirely. For a human-friendly pointer that moves over time, use a **Lambda alias** pointing at a published version, not a mutable image tag. Aliases move; the image stays put.

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

### Scan in ECR (post-push)

Because the repository was created with `scanOnPush=true`, ECR automatically starts a vulnerability scan as soon as the image lands. ECR offers two scan types with very different coverage — it's worth understanding which you're using.

#### Basic vs. enhanced scanning

| Feature | Basic scanning (default) | Enhanced scanning |
|---|---|---|
| Scanner | ECR-native (Clair-based) | Amazon Inspector |
| OS packages | ✅ | ✅ |
| Language libraries (pip, npm, Maven, Go, Rust…) | ❌ | ✅ |
| Continuous re-scan as new CVEs appear | ❌ | ✅ |
| Findings API | `ecr describe-image-scan-findings` | `inspector2 list-findings` |
| Console | ECR → image → Scan findings tab | Amazon Inspector → Findings |
| Cost | Free | Per-image + per-monitoring-day |

Basic scanning is what `scanOnPush=true` gives you by default. Enhanced scanning is opt-in at the registry level.

#### Retrieve basic-scan findings

```bash
# Start a scan manually (optional — scanOnPush already triggers one)
aws ecr start-image-scan \
  --repository-name ${ECR_REPO} \
  --image-id imageTag=${IMAGE_TAG} \
  --region ${AWS_REGION}

# Check scan status — returns IN_PROGRESS, COMPLETE, or FAILED
aws ecr describe-image-scan-findings \
  --repository-name ${ECR_REPO} \
  --image-id imageTag=${IMAGE_TAG} \
  --region ${AWS_REGION} \
  --query 'imageScanStatus'

# Summary of findings by severity
aws ecr describe-image-scan-findings \
  --repository-name ${ECR_REPO} \
  --image-id imageTag=${IMAGE_TAG} \
  --region ${AWS_REGION} \
  --query 'imageScanFindings.findingSeverityCounts'

# Full findings as a table (severity, CVE ID, NVD link)
aws ecr describe-image-scan-findings \
  --repository-name ${ECR_REPO} \
  --image-id imageTag=${IMAGE_TAG} \
  --region ${AWS_REGION} \
  --query 'imageScanFindings.findings[*].[severity, name, uri]' \
  --output table
```

The initial scan takes 30–60 seconds. If the status is `IN_PROGRESS`, wait and retry.

#### Enable enhanced scanning

If basic scanning reports no findings on an image you suspect is vulnerable (especially one with Python/Node/Go dependencies), it's because basic scanning doesn't look inside language ecosystems. Turn on enhanced scanning:

```bash
# Enable Amazon Inspector for ECR in this region (one-time)
aws inspector2 enable \
  --resource-types ECR \
  --region ${AWS_REGION}

# Tell ECR to use enhanced scanning on push for all repositories
aws ecr put-registry-scanning-configuration \
  --scan-type ENHANCED \
  --rules '[{"scanFrequency":"SCAN_ON_PUSH","repositoryFilters":[{"filter":"*","filterType":"WILDCARD"}]}]' \
  --region ${AWS_REGION}
```

Push the image again so Inspector scans it:

```bash
docker push ${REGISTRY}/${ECR_REPO}:${IMAGE_TAG}
```

#### Retrieve enhanced-scan findings (Amazon Inspector)

Enhanced findings do **not** appear in `describe-image-scan-findings`. They live in Inspector:

```bash
aws inspector2 list-findings \
  --filter-criteria '{"ecrImageTags":[{"comparison":"EQUALS","value":"'"${IMAGE_TAG}"'"}],"ecrImageRepositoryName":[{"comparison":"EQUALS","value":"'"${ECR_REPO}"'"}]}' \
  --region ${AWS_REGION} \
  --query 'findings[*].[severity, title, packageVulnerabilityDetails.vulnerablePackages[0].name, packageVulnerabilityDetails.vulnerablePackages[0].version]' \
  --output table
```

Filter to critical and high only:

```bash
aws inspector2 list-findings \
  --filter-criteria '{"ecrImageTags":[{"comparison":"EQUALS","value":"'"${IMAGE_TAG}"'"}],"ecrImageRepositoryName":[{"comparison":"EQUALS","value":"'"${ECR_REPO}"'"}],"severity":[{"comparison":"EQUALS","value":"CRITICAL"},{"comparison":"EQUALS","value":"HIGH"}]}' \
  --region ${AWS_REGION} \
  --output table
```

Or view findings in the console: **Amazon Inspector → Findings → filter by ECR repository**.

### Scout vs. Inspector — interpreting the differences

Running Scout and Inspector on the same image often produces different findings. That's expected, and understanding why is one of the most important lessons of this workshop.

| | Docker Scout | Amazon Inspector (enhanced ECR scan) |
|---|---|---|
| **When** | Locally, pre-push, or in CI | In ECR, post-push + continuous |
| **Coverage** | OS packages + language libraries + indirect deps | OS packages + language libraries |
| **Data sources** | GitHub Advisory DB, NVD, OSV, vendor feeds | AWS-curated feeds backed by Inspector |
| **Philosophy** | Full CVE inventory — reports everything found | Actionable findings — reports what you can fix |
| **CI integration** | `docker scout cves --exit-code` fails the build | EventBridge events → SNS, Slack, ticketing |

#### Why Inspector may report fewer CVEs than Scout

Inspector deliberately suppresses individual CVEs when there's no fix available and instead emits a higher-level finding. The most common example:

> **Platform End Of Life** (severity: CRITICAL)

This appears when the base image's OS is no longer receiving security updates from its distribution (for example, `node:12` on Debian Stretch, EOL June 2022). Inspector's reasoning: listing individual libxslt/libcurl/openssl CVEs isn't useful if the fix is *"replace the whole base image"*. One clear finding is louder than 40 individual ones.

Scout takes the opposite approach — it lists every CVE from public feeds regardless of whether a distro-supplied fix exists. So you'll often see Scout report 4–40 CVEs while Inspector reports 1 "Platform EOL" finding that covers all of them.

#### Which is "right"?

Both. They answer different questions:

- **Scout** — *"What CVEs are present in this image?"* (full inventory)
- **Inspector** — *"What should I do about them?"* (prioritised, actionable)

In practice:
- Use **Scout as a pre-push gate** to fail builds on known-fixable critical/high CVEs.
- Use **Inspector for continuous monitoring** so you hear about CVEs disclosed after push and get a clean list of what's actually fixable.
- Treat **Platform EOL findings as the top priority** — they usually subsume dozens of individual CVEs, and the fix (upgrade the base image) clears the lot.

#### Teaching demo: vulnerable vs. clean base

The quickest way to show this trade-off is to scan an image with an old base, then one with a current base:

```bash
# Vulnerable: old base image on an EOL distro
# FROM node:12           → Debian Stretch (EOL)
# FROM python:3.8-slim   → Debian Buster (EOL)

# Clean: current base
# FROM node:20           → Debian Bookworm
# FROM public.ecr.aws/lambda/python:3.12  → Amazon Linux 2023
```

Rebuild, push, re-scan with both tools. Scout's CVE count drops drastically. Inspector's "Platform EOL" finding disappears entirely. Both scanners agree the image is in much better shape.

### Set a lifecycle policy (recommended)

Auto-delete untagged images after seven days so ECR doesn't fill up with orphan layers. The policy document is already in [`lifecycle.json`](./lifecycle.json):

```bash
aws ecr put-lifecycle-policy \
  --repository-name ${ECR_REPO} \
  --lifecycle-policy-text file://lifecycle.json
```

## 4. Create the Lambda function

### IAM role (one-time)

The Lambda execution role's trust policy is already in [`trust-policy.json`](./trust-policy.json):

```bash
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
  --architectures arm64 \
  --region ${AWS_REGION}
```

`--package-type Image` tells Lambda to pull from ECR. The image URI must be in the same region as the function. `--architectures arm64` must match the architecture of the image you pushed (see section 1). Architecture cannot be changed after the function is created — you'd need to delete and recreate.

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

The workflow in [`.github/workflows/deploy.yml`](./.github/workflows/deploy.yml) automates everything above. It triggers on a published GitHub Release (or manual dispatch) and does:

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

### One-time setup — AWS OIDC provider and IAM roles

Before the workflow can assume an AWS role from GitHub, the account needs:

1. A GitHub Actions OIDC provider (`token.actions.githubusercontent.com`).
2. An IAM role for ECR push (scoped to your repo).
3. An IAM role for Lambda deploy (scoped to your repo and function).

Both roles trust only the specific GitHub repository via the `sub` claim, so they can't be assumed from any other repo.

#### Deploy the CloudFormation stack

A ready-made template lives at [`infra/github-oidc-roles.yaml`](./infra/github-oidc-roles.yaml). Deploy it:

```bash
export GITHUB_ORG=<your-github-org-or-user>
export GITHUB_REPO=<your-repo-name>

aws cloudformation deploy \
  --stack-name hello-lambda-github-oidc \
  --template-file infra/github-oidc-roles.yaml \
  --region ${AWS_REGION} \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubOrg=${GITHUB_ORG} \
    GitHubRepo=${GITHUB_REPO} \
    EcrRepositoryName=${ECR_REPO} \
    LambdaFunctionName=${FUNCTION_NAME}
```

If the GitHub OIDC provider already exists in the account (only one can exist per account), add `CreateOidcProvider=false` to the `--parameter-overrides`.

#### Read the role ARNs from the stack outputs

The stack exposes three outputs: `EcrPushRoleArn`, `LambdaDeployRoleArn`, and `EcrRepoVariable`. Capture them into shell variables so they're easy to paste into GitHub afterwards:

```bash
export AWS_ROLE_TO_ASSUME_ECR=$(aws cloudformation describe-stacks \
  --stack-name hello-lambda-github-oidc \
  --region ${AWS_REGION} \
  --query "Stacks[0].Outputs[?OutputKey=='EcrPushRoleArn'].OutputValue" \
  --output text)

export AWS_ROLE_TO_ASSUME_LAMBDAS=$(aws cloudformation describe-stacks \
  --stack-name hello-lambda-github-oidc \
  --region ${AWS_REGION} \
  --query "Stacks[0].Outputs[?OutputKey=='LambdaDeployRoleArn'].OutputValue" \
  --output text)

# Print values ready to paste into GitHub repository variables
echo "AWS_ROLE_TO_ASSUME_ECR=${AWS_ROLE_TO_ASSUME_ECR}"
echo "AWS_ROLE_TO_ASSUME_LAMBDAS=${AWS_ROLE_TO_ASSUME_LAMBDAS}"
echo "ECR_REPO=${ECR_REPO}"
```

Or view all outputs as a table:

```bash
aws cloudformation describe-stacks \
  --stack-name hello-lambda-github-oidc \
  --region ${AWS_REGION} \
  --query 'Stacks[0].Outputs' \
  --output table
```

### GitHub repository variables

The workflow reads three repository variables (not secrets — these are ARNs, not credentials):

| Variable | Value |
|---|---|
| `AWS_ROLE_TO_ASSUME_ECR` | ARN of `github-ecr-push` (from stack output) |
| `AWS_ROLE_TO_ASSUME_LAMBDAS` | ARN of `github-lambda-deploy` (from stack output) |
| `ECR_REPO` | `hello-lambda` |

**With the GitHub CLI (fastest):**

This assumes you exported `AWS_ROLE_TO_ASSUME_ECR` and `AWS_ROLE_TO_ASSUME_LAMBDAS` when reading the stack outputs in the previous step. Replace `<your-username>` with your GitHub handle (the fork's owner).

```bash
export FORK_REPO=<your-username>/docker-lambda-workshop

gh variable set AWS_ROLE_TO_ASSUME_ECR \
  --repo ${FORK_REPO} \
  --body "${AWS_ROLE_TO_ASSUME_ECR}"

gh variable set AWS_ROLE_TO_ASSUME_LAMBDAS \
  --repo ${FORK_REPO} \
  --body "${AWS_ROLE_TO_ASSUME_LAMBDAS}"

gh variable set ECR_REPO \
  --repo ${FORK_REPO} \
  --body "${ECR_REPO}"

# Verify
gh variable list --repo ${FORK_REPO}
```

**Or via the GitHub UI:**

Navigate to your fork → **Settings** → **Secrets and variables** → **Actions** → **Variables** tab → **New repository variable**. Add the three variables above one at a time.

### Workflow location

The workflow lives at `.github/workflows/deploy.yml` at the root of this repository — GitHub only discovers workflows under that path. The `context: .` in the build step points Docker at the repo root where the `Dockerfile` lives.

### Test the pipeline end-to-end

Once the CloudFormation stack is deployed and the three GitHub variables are set, run through the workflow to confirm everything wires up correctly.

#### 1. Confirm your fork is ready

You already forked and cloned the workshop repo at the start. Two things still need to happen on your fork before the workflow can run:

**Deploy the CloudFormation stack (if you haven't already).** Follow the "One-time setup — AWS OIDC provider and IAM roles" steps above. The IAM role's trust policy is scoped to exactly your fork's `owner/repo`, so the stack must be deployed with your values.

**Enable Actions on the fork.** Forked repos have GitHub Actions disabled by default, and there's no `gh` command to enable them — this must be done through the UI. On your fork's page:

1. Go to the **Actions** tab
2. Click **I understand my workflows, go ahead and enable them**

Without this step, the workflow won't run when triggered.

#### 2. Trigger the workflow manually

A manual run is the fastest way to iterate before cutting a release.

1. Open the repo on GitHub
2. Go to the **Actions** tab
3. Select **Deploy hello-lambda** in the left sidebar
4. Click **Run workflow** (top right)
5. Leave `image_tag` empty — it will default to the current commit SHA
6. Click the green **Run workflow** button

Or trigger from the CLI with [`gh`](https://cli.github.com/):

```bash
gh workflow run "Deploy hello-lambda" --repo ${GITHUB_ORG}/${GITHUB_REPO}
```

#### 3. Watch the run

In the Actions tab, click the running workflow to see both jobs stream their logs. What to check at each step:

| Step | What to look for |
|---|---|
| **Configure AWS credentials** | `Authenticated as arn:aws:sts::...:assumed-role/github-ecr-push/...` — proves OIDC works |
| **Login to Amazon ECR** | `Login Succeeded` |
| **Build and push** | layers being cached; final `pushing manifest ...` line |
| **Scan image with Docker Scout** | CVE findings printed; job continues even if critical/high are found (advisory mode) |
| **Deploy / Update Lambda function code** | `LastUpdateStatus: Successful` in the JSON output |
| **Wait for update to finish** | returns with exit code 0 in under a minute |

If a step fails, the error message usually points straight at the cause — most first-run failures are missing GitHub variables, the OIDC trust condition not matching the repo name, or the IAM role missing a permission.

#### 4. Verify the Lambda was updated

After the workflow goes green:

```bash
aws lambda get-function \
  --function-name ${FUNCTION_NAME} \
  --query 'Code.ImageUri' \
  --output text
```

The image URI should end with the commit SHA of the run you just triggered. Confirm the function still works:

```bash
aws lambda invoke \
  --function-name ${FUNCTION_NAME} \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

#### 5. Trigger on release (once manual runs work)

Once the pipeline is stable, switch to release-driven deploys — the production pattern:

1. Repo → **Releases** → **Draft a new release**
2. Create a new tag (e.g. `v1.0.0`)
3. Write release notes
4. Click **Publish release**

The workflow fires automatically on publication. Tagged releases give you an immutable pointer to a specific commit, which is better than "whatever `main` is right now".

#### Common troubleshooting

**OIDC role can't be assumed — `AccessDenied`**
The trust policy's `sub` claim probably doesn't match. Check: does the role trust `repo:${GITHUB_ORG}/${GITHUB_REPO}:*` where those values match exactly what's in the GitHub URL? Case-sensitive.

**`docker push` fails with `denied: User is not authorized`**
The `github-ecr-push` role's policy scopes ECR actions to `repository/${ECR_REPO}`. If your ECR_REPO variable in GitHub doesn't match the repo name used in CloudFormation, the role has no permission for the actual repo.

**Lambda update fails with `ResourceNotFoundException`**
Either the Lambda function doesn't exist yet (run section 4 first), or `FUNCTION_NAME` in the stack parameters doesn't match the actual function name.

**Scout step reports no findings but shows as failed**
Docker Scout requires a Docker Hub account. The workflow doesn't authenticate to Docker Hub, so scout-action may hit rate limits on heavy workshop days. If this happens, that's another reason to leave `continue-on-error: true` in place until you add a `docker/login-action` step.

### arm64 runners

Both jobs run on `ubuntu-24.04-arm` (public preview, free for public repos). Native arm64 runners avoid QEMU emulation and keep the workflow consistent with the arm64 Lambda. If you switch to a private repo, check GitHub's pricing for arm64 runners — they may incur extra billing.

### Scout is advisory for now

The Docker Scout step has `continue-on-error: true` set. If Scout finds critical or high CVEs, the workflow prints the findings but **doesn't fail the build** — the image still gets pushed and deployed.

This is deliberate for first-run stability. Once the pipeline works end-to-end and you're confident in the base image baseline, remove `continue-on-error: true` from the workflow:

```yaml
- name: Scan image with Docker Scout
  uses: docker/scout-action@v1
  # continue-on-error: true    ← delete this line
  with:
    command: cves
    ...
    exit-code: true
```

With the flag gone, `exit-code: true` makes Scout fail the workflow when critical or high CVEs are found, which is the real security gate. Treat the advisory period as a migration ramp, not a permanent state.

### Why post-push ECR scanning isn't in the workflow

The repository is created with `scanOnPush=true`, so ECR starts a vulnerability scan automatically every time an image lands. The workflow deliberately doesn't add an `aws ecr start-image-scan` step because:

- **It would duplicate `scanOnPush`.** Scan-on-push already handles every deploy.
- **Scout has already gated the build.** Critical and high CVEs fail the pipeline before the image reaches ECR.
- **Polling ECR for results in CI is fragile.** Scans take 30–60 seconds; wait loops and timeouts add complexity for a check Scout already performed.
- **Post-push scanning is a monitoring concern, not a deploy concern.** Its value is catching CVEs disclosed *after* the image has been sitting in ECR — something a deploy pipeline can't observe.

For continuous post-push monitoring, wire ECR scan findings to EventBridge and route critical/high severities to SNS, Slack, or a ticketing system. That pattern fits the session 4 observability conversation, not the deploy workflow.

### What this workflow intentionally skips

Kept out to stay teachable. In production you'd want:

- A GitHub Environment with required reviewers gating `deploy`.
- Lambda aliases and versions instead of overwriting `$LATEST`.
- A post-deploy smoke test that invokes the function and asserts the response.

## 7. Alternative CI/CD — AWS CodePipeline

GitHub Actions is the quickest CI/CD on-ramp, but teams already using AWS CodePipeline can drive the same flow without leaving the AWS account. This section builds the equivalent pipeline: GitHub source via CodeStar Connections → CodeBuild (build + push to ECR + update Lambda) → back to GitHub for the next commit.

The result is functionally identical to section 6, just with a different orchestrator. Pick whichever fits your operating model.

### Why you might prefer CodePipeline

- All stages, permissions, and logs live in AWS — no GitHub Actions minutes, no arm64 runner billing concerns.
- CodeStar Connections replace long-lived OAuth tokens with a managed GitHub App.
- CodeBuild's `aarch64` images build arm64 Lambda images natively — no QEMU, no emulation.
- Easy to extend with CodeBuild-based security gates or approval actions later.

### Pipeline shape

```
GitHub push  ➜  CodeStar Connection  ➜  CodePipeline
                                           │
                                           ├─ Source stage  (fetch repo)
                                           └─ BuildAndDeploy stage
                                                └─ CodeBuild:
                                                    • docker build --platform linux/arm64
                                                    • docker push  <acct>.dkr.ecr.<region>…/hello-lambda:<sha>
                                                    • aws lambda update-function-code
                                                    • aws lambda wait function-updated
```

Everything lives in two files:

- [`infra/lambda-codepipeline.yaml`](./infra/lambda-codepipeline.yaml) — CloudFormation creating the pipeline, CodeBuild project, IAM roles, and S3 artefact bucket
- [`infra/buildspec.yml`](./infra/buildspec.yml) — CodeBuild buildspec the pipeline points at

### 1. Create the CodeStar Connection to GitHub

CodeStar Connections is AWS's modern, token-free way to link GitHub and AWS services. Create the connection:

```bash
export CONNECTION_ARN=$(aws codestar-connections create-connection \
  --provider-type GitHub \
  --connection-name hello-lambda-github \
  --region ${AWS_REGION} \
  --query 'ConnectionArn' \
  --output text)

echo "CONNECTION_ARN=${CONNECTION_ARN}"
```

A new connection starts in `PENDING` state. Finish authorising it in the AWS Console:

1. Open **Developer Tools → Settings → Connections** in the AWS Console (same region)
2. Click the new `hello-lambda-github` connection
3. Click **Update pending connection**
4. Sign in to GitHub if prompted, then click **Install a new app**
5. On the installation page, choose **Only select repositories** and pick `docker-lambda-workshop`
6. Click **Install & Authorize**
7. Back in AWS, click **Connect**

The connection status should flip to **Available**. Confirm:

```bash
aws codestar-connections get-connection \
  --connection-arn ${CONNECTION_ARN} \
  --region ${AWS_REGION} \
  --query 'Connection.ConnectionStatus' \
  --output text
```

Expected: `AVAILABLE`.

### 2. Deploy the pipeline stack

```bash
# Re-export if you opened a new shell
export GITHUB_ORG=<your-github-username-or-org>

aws cloudformation deploy \
  --stack-name hello-lambda-codepipeline \
  --template-file infra/lambda-codepipeline.yaml \
  --region ${AWS_REGION} \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    GitHubOwner=${GITHUB_ORG} \
    GitHubRepo=docker-lambda-workshop \
    GitHubBranch=main \
    CodeStarConnectionArn=${CONNECTION_ARN} \
    EcrRepositoryName=${ECR_REPO} \
    LambdaFunctionName=${FUNCTION_NAME}
```

The stack creates a CodeBuild project running on `aws/codebuild/amazonlinux2-aarch64-standard:3.0` — an arm64 image that builds arm64 Lambda images natively.

Prerequisites the stack expects to already exist (created in sections 3 and 4):

- The ECR repository `${ECR_REPO}`
- The Lambda function `${FUNCTION_NAME}`

If either is missing, go back and run the earlier sections first.

### 3. Watch the first run

The pipeline triggers automatically on the first run since it's configured with `DetectChanges: true`. Open it:

```bash
aws cloudformation describe-stacks \
  --stack-name hello-lambda-codepipeline \
  --region ${AWS_REGION} \
  --query "Stacks[0].Outputs[?OutputKey=='PipelineUrl'].OutputValue" \
  --output text
```

Or from the terminal:

```bash
PIPELINE_NAME=$(aws cloudformation describe-stacks \
  --stack-name hello-lambda-codepipeline \
  --region ${AWS_REGION} \
  --query "Stacks[0].Outputs[?OutputKey=='PipelineName'].OutputValue" \
  --output text)

aws codepipeline get-pipeline-state \
  --name ${PIPELINE_NAME} \
  --region ${AWS_REGION} \
  --query 'stageStates[*].{Stage:stageName,Status:latestExecution.status}' \
  --output table
```

### 4. Trigger a deploy

Any push to the tracked branch (`main` by default) triggers the pipeline. To re-run the latest without pushing:

```bash
aws codepipeline start-pipeline-execution \
  --name ${PIPELINE_NAME} \
  --region ${AWS_REGION}
```

Or use the **Release change** button in the console.

### 5. Verify the Lambda was updated

The buildspec updates Lambda as the last step of the build. Confirm:

```bash
aws lambda get-function \
  --function-name ${FUNCTION_NAME} \
  --query 'Code.ImageUri' \
  --output text
```

The image URI should end with the first seven characters of the commit SHA that triggered the run.

Invoke the function to confirm it still works:

```bash
aws lambda invoke \
  --function-name ${FUNCTION_NAME} \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

### CodePipeline vs. GitHub Actions — trade-offs

| | GitHub Actions (section 6) | CodePipeline (this section) |
|---|---|---|
| Where it runs | GitHub-hosted runner | AWS CodeBuild |
| AWS auth | OIDC federation (no secrets) | IAM service role (in-account) |
| Source integration | Native (same platform) | CodeStar Connection to GitHub |
| arm64 builds | `ubuntu-24.04-arm` runner | CodeBuild `aarch64` image |
| Cost | Free for public repos, minute-based for private | CodeBuild per build-minute + S3 + CodeStar (no extra) |
| Logs | GitHub Actions UI | CloudWatch Logs + CodePipeline console |
| Audit trail | GitHub | CloudTrail |
| Best when | Team already lives in GitHub | AWS-first operating model |

Neither is better in general — they're the same pipeline expressed in two ecosystems. For the workshop, running both gives attendees direct experience with each and a basis for picking what fits their team.

### What this pipeline intentionally skips

To stay focused on the core flow, the pipeline omits:

- **A separate scan stage.** Scout/Inspector integration is left as an exercise (a second CodeBuild action between Source and BuildAndDeploy, failing on critical/high CVEs).
- **Manual approval before deploy.** A `AWS::CodePipeline::Pipeline` approval action would gate production.
- **Multi-environment promotion.** Production-grade pipelines usually have dev → staging → prod stages with alias shifting.

All of these drop in cleanly as additional CodePipeline stages if you need them later.

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
