# Storage providers

agent2kb stores everything in one S3-compatible bucket, using key prefixes
(`kbs/<name>/raw|wiki|outputs/` for KBs created with `a2kb new`). It uses only the standard
S3 API, so other S3-compatible providers should work, but only the providers marked
**Tested** below have been verified against a real server. Run `a2kb doctor` after editing your config: it
checks the endpoint, credentials, bucket, list and write access, and tells you what to fix.

## Where the config lives

The full description is in [configuration](configuration.md). In short:

- **User-level**: `~/.kb/config.yaml` (or `$KB_CONFIG`), format v2: `storage:` (the
  connection shared by your KBs), `llm:` and `kbs:`. Written by `a2kb setup` and
  `a2kb new`. It is the place for keys and for admin KBs (`role: admin`).
- **Project-level**: `.kb/config.yaml` in a project directory (agent2kb looks in the current
  directory and its parents). It is a pointer with no secrets that selects a KB from the
  user-level file:

  ```yaml
  kb: notes
  ```

  Inside that directory `a2kb` picks the KB from `kb:`. Elsewhere pass `-p notes` or set
  `KB_PROFILE=notes`.

Settings are merged per field: command-line flags, then `KB_*` environment variables, then the
project config, then the user-level config. Older flat and `profiles:` files still work; see
[Legacy profiles](#legacy-profiles).

## Test status

| Provider | Status |
|---|---|
| SeaweedFS | **Tested** (local throwaway server, `a2kb doctor` + integration tests in `tests/integration/`) |
| AWS S3 | Unit-tested against moto only (bucket create/region handling, credential chain), not tested against real AWS; the [recipe](#recipe-a-private-aws-bucket-with-one-kb) has not been run end to end |
| Cloudflare R2, Backblaze B2, MinIO | **Untested**: settings below follow each provider's documentation; the R2 recipe has not been run end to end |
| Wasabi | **Untested** |

Reports from real deployments are welcome.

## Storage fields

`a2kb setup` fills these in for you (presets: aws, r2, b2, minio, seaweedfs, wasabi, other).
The snippets below show the `storage:` block of a v2 config; a KB may carry its own
`storage:` block (typically only its own keys) that overrides the shared one.

| Field | Env var | Meaning |
|---|---|---|
| `endpoint` | `KB_S3_ENDPOINT` | Endpoint URL. Leave out for AWS S3. |
| `access_key` / `secret_key` | `KB_ACCESS_KEY` / `KB_SECRET_KEY` | Set both, or leave both empty to use boto3's default credential chain (`AWS_ACCESS_KEY_ID` env, `~/.aws/credentials`, IAM role). One without the other is an error. The chain is only allowed from the user-level config or env, see below. |
| `aws_profile` | `KB_AWS_PROFILE` | Named profile in `~/.aws/config` to take credentials from. User-level config or env only; rejected in a project `.kb/config.yaml`. |
| `bucket` | `KB_BUCKET` | Bucket name. 3-63 chars, lowercase letters, digits, `-` and `.`. |
| `region` | `KB_S3_REGION` | Region name. Use `auto` for Cloudflare R2. Unset: boto3 default. |
| `addressing_style` | `KB_S3_ADDRESSING_STYLE` | `auto`, `path` or `virtual`. Unset: boto3 default. MinIO and most self-hosted servers want `path`. |
| `checksum_mode` | `KB_S3_CHECKSUM_MODE` | `when_supported` or `when_required`. Unset: boto3's default for every endpoint. |

The KB's `prefix` (env `KB_PREFIX`) is not a storage field: it defaults to `kbs/<name>/` and is
set on the KB entry. Only an admin KB may have an empty prefix.

`checksum_mode` exists because boto3 1.36+ adds CRC32 checksums to every upload, and some
S3-compatible servers reject them (typically with `XAmzContentSHA256Mismatch`,
`InvalidRequest` or `NotImplemented`). It is **not** set automatically: if uploads to your
provider fail that way, add `checksum_mode: when_required`. The snippets below include it
where providers have been reported to need it, and the `a2kb setup` wizard writes it
explicitly for every non-AWS preset.

## Where credentials may come from

If `access_key` and `secret_key` are both empty, agent2kb signs requests with whatever AWS
credentials your machine has (environment, `~/.aws`, an IAM role, or `aws_profile`). A project
`.kb/config.yaml` may come from a repository you cloned, so it is not trusted to choose that.
The rules:

- `aws_profile` can be set in the user-level config or `KB_AWS_PROFILE`, never in a project
  config.
- A KB defined in your v2 file takes only a `kb: <name>` pointer in a project folder; a
  project file that carries storage settings for it is an error.
- A project config in the old flat format may set the storage target (`s3_endpoint`,
  `region`, `bucket`, `prefix`, `addressing_style`) **only together with its own `access_key`
  and `secret_key`**. It can never make agent2kb sign with keys from the user-level config,
  or with the ambient AWS credentials, against a host, bucket or prefix it names: a cloned
  repo could otherwise send your keys to another server, or write your data into someone
  else's bucket or into another KB's prefix.
- Keys you pass yourself (`KB_ACCESS_KEY` + `KB_SECRET_KEY`, or `--access-key` +
  `--secret-key`) count as your choice, but only for a project-chosen `bucket`, `prefix` or
  `addressing_style`. They are never sent to a project-chosen endpoint or region unless you
  set the endpoint/region yourself (`KB_S3_ENDPOINT`, `KB_S3_REGION`, or the flags).
- A non-admin KB may not have an empty or `/` prefix.
- An empty string in a project config is ignored; it does not blank out a user-level value.

Put KBs that use ambient AWS credentials in `~/.kb/config.yaml`.

## AWS S3

```yaml
version: 2
storage:
  bucket: my-kb-bucket
  region: eu-west-1
  # No keys: uses env / ~/.aws / IAM role. Or pin a profile:
  # aws_profile: kb
  # Or static keys:
  # access_key: AKIA...
  # secret_key: ...
kbs:
  notes: {}            # prefix kbs/notes/
```

### Recipe: a private AWS bucket with one KB

> **Status:** written from the AWS documentation linked below and from agent2kb's code. It has
> **not yet been run end to end against real AWS** (see [Test status](#test-status)). The
> policy uses only the S3 calls agent2kb makes (listing with a prefix, get, head, put and
> delete of objects, plus `HeadBucket` and `CreateBucket` during setup).

Two kinds of identity are involved; keep them apart:

| Identity | Used for | Needs |
|---|---|---|
| **Provisioning** (your administrator login) | Creating the bucket, the IAM users and policies | Broad S3 and IAM rights. Not stored in the KB config. |
| **Runtime**, one per KB | Everything agent2kb does day to day for that KB (ingest, compile, query, cron) | The KB policy below |
| **Device**, one per Obsidian device | Remotely Save ([obsidian](obsidian.md)) | The KB policy below (editable), or list and read only (viewing) |

**1. Create the bucket.** In the S3 console, create a general purpose bucket in the region you
want, keep Block Public Access enabled, and note the bucket name and the region code (for
example `eu-west-1`). Official steps:
[Creating a bucket](https://docs.aws.amazon.com/AmazonS3/latest/userguide/create-bucket-overview.html).
Alternatively `a2kb setup` (or `a2kb doctor --fix`) offers to create it with the credentials
you give it, which must then be allowed to create buckets.

**2. Decide how agent2kb gets credentials.** agent2kb uses boto3, so it can sign with a static
key pair or with anything in boto3's credential chain:

| Option | How | Suits |
|---|---|---|
| Static key of an IAM user | `access_key` and `secret_key` in the KB's `storage:` block (the wizard asks for them) | A server without an IAM role; one key per KB |
| AWS credential chain | No keys in the config: environment variables, `~/.aws/credentials`, an IAM role of the machine | An EC2 instance or container with a role |
| Named AWS profile | `aws_profile: <name>` (user-level config or `KB_AWS_PROFILE` only): a profile in `~/.aws/config`, including IAM Identity Center (SSO, after `aws sso login`) and assume-role profiles | Interactive use on a workstation |

AWS recommends temporary credentials (roles, IAM Identity Center) over long-term access keys;
see [Manage access keys for IAM users](https://docs.aws.amazon.com/IAM/latest/UserGuide/id_credentials_access-keys.html)
and boto3's [credential chain](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html).
An SSO session expires and must be renewed with `aws sso login`, so unattended cron jobs are
better served by a role or a dedicated key. Whatever the machine uses, Obsidian devices get
their own key (step 6).

**3. Create the runtime policy for the KB.** Replace `my-kb-bucket` and `kbs/notes/`:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOnlyTheKbPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::my-kb-bucket",
      "Condition": {"StringLike": {"s3:prefix": ["kbs/notes/*"]}}
    },
    {
      "Sid": "ObjectsInTheKbPrefix",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-kb-bucket/kbs/notes/*"
    }
  ]
}
```

`s3:ListBucket` is granted on the **bucket** ARN and limited by the `s3:prefix` condition; the
object actions are granted on the **object** ARN under the prefix. AWS explains the condition
in [Bucket policy examples using condition keys](https://docs.aws.amazon.com/AmazonS3/latest/userguide/amazon-s3-policy-keys.html)
(agent2kb lists with `ListObjectsV2`, which needs `s3:ListBucket` also on a versioned bucket;
`s3:ListBucketVersions` is needed only to list object versions, which agent2kb never does; see
[ListObjectsV2](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectsV2.html) and
[ListObjectVersions](https://docs.aws.amazon.com/AmazonS3/latest/API/API_ListObjectVersions.html)). Because a copy reads
from one place and writes to another, a scoped key must not have read access to anything
outside its prefix. Attach the policy to an IAM user (or role) for this KB and, for a static
key, create an access key for that user. The secret is shown only once, when it is created.

**4. Run the wizard with the KB's runtime key.** The bucket must exist (step 1): a key limited
to one prefix cannot create it.

```bash
a2kb setup        # preset aws; region; bucket; "n" to the credential chain; the runtime key
```

A prefix-scoped key is denied `HeadBucket`: that request takes no prefix and requires
`s3:ListBucket` on the whole bucket
([HeadBucket](https://docs.aws.amazon.com/AmazonS3/latest/API/API_HeadBucket.html)).
`a2kb setup`, `a2kb new` and `a2kb doctor` treat that 403 as "cannot inspect the bucket", list
the KB's own prefix instead, and continue if that works.

To use a key for a further KB in the same bucket, give that KB its own `storage:` block in
`~/.kb/config.yaml` (mode 0600), which overrides the shared connection for that KB only:

```yaml
kbs:
  notes: {}
  alpha:
    storage: {access_key: AKIA..., secret_key: ...}   # a key limited to kbs/alpha/
```

**5. Check.** Run `a2kb -p notes doctor`: the prefix listing and the write probe must pass.
Another KB's prefix must stay inaccessible to this key (for example a listing of `kbs/alpha/`
with the AWS CLI and the `notes` key fails with `AccessDenied`).

**6. Obsidian.** Create a separate IAM user (and access key) per device with the same policy
for editing, or with only `s3:ListBucket` (same condition) and `s3:GetObject` for viewing.
`a2kb connect obsidian notes` prints the endpoint (`https://s3.<region>.amazonaws.com`),
region, bucket and prefix; with the credential chain it shows placeholders for the device key.
See [obsidian](obsidian.md).

**7. First page.** Continue with the [first-run tutorial](first-run.md).

## Cloudflare R2

```yaml
version: 2
storage:
  endpoint: https://<account-id>.r2.cloudflarestorage.com
  region: auto
  access_key: <R2 access key id>
  secret_key: <R2 secret access key>
  bucket: kb-notes
  checksum_mode: when_required
kbs:
  notes: {}
```

`region: auto` is R2's placeholder (R2 has no regions to choose). R2 API tokens can be limited
to a bucket but **not to a prefix**. Use **one bucket per KB** if KBs must not see each
other's data; the prefix inside is then merely a convention. A two-KB example with a bucket
and a key per KB is in [configuration](configuration.md#several-kbs-with-separate-buckets-r2).

### Recipe: a private R2 bucket with one KB

> **Status:** written from Cloudflare's
> [R2 S3 quickstart](https://developers.cloudflare.com/r2/get-started/s3/) and
> [R2 authentication](https://developers.cloudflare.com/r2/api/tokens/) pages and from
> agent2kb's code. It has **not yet been run end to end against real R2**. Dashboard labels
> change; follow the linked pages where they differ.

**1. Create the bucket.** In the Cloudflare dashboard open **R2 object storage**, choose
**Create bucket**, enter a name (for example `kb-notes`) and choose its location. Or with
Wrangler: `npx wrangler r2 bucket create kb-notes`. Do not connect a public domain or enable
public access: Obsidian and agent2kb use the S3 API with keys.

**2. Create S3 credentials for the bucket.** On the R2 page, next to **API Tokens**, choose
**Manage**, then **Create Account API token** or **Create User API token** (an account token
belongs to the account; a user token stops working if your user is removed from the account).
Choose the permission **Object Read & Write**, apply it to **specific buckets only**, and select
the bucket. After creating it, copy the **Access Key ID** and the **Secret Access Key**: these
are the pair the wizard asks for. The secret is shown only once. A Cloudflare API token value
used as a bearer token is not this pair and does not work here.

Permissions in short: **Object Read & Write** reads, writes and lists objects in the selected
buckets; **Object Read only** reads and lists; creating buckets needs an **Admin** permission.
So with an object token, create the bucket in the dashboard (step 1) and do not rely on
`--create-bucket` or `a2kb doctor --fix`.

**3. Note the endpoint.** The S3 API endpoint is
`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`; the dashboard shows it on the R2 overview
page and at the end of token creation. Use this S3 endpoint, never a public bucket URL or a
custom domain. A bucket created in a jurisdiction is reachable only through that
jurisdiction's endpoint, for example `https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com` (EU)
or `https://<ACCOUNT_ID>.fedramp.r2.cloudflarestorage.com` (FedRAMP). The `r2` preset builds
the default endpoint from the account ID; for a jurisdictional bucket pass the endpoint
explicitly with `--endpoint`.

**4. Run the wizard.**

```bash
a2kb setup                                   # preset r2; account ID, bucket; "n" to the
                                             # credential chain; then the key pair
# a jurisdictional bucket, or without prompts:
KB_ACCESS_KEY=... KB_SECRET_KEY=... a2kb setup --non-interactive --preset r2 \
  --endpoint https://<ACCOUNT_ID>.eu.r2.cloudflarestorage.com --bucket kb-notes \
  --llm claude --kb-name notes
```

The wizard writes `region: auto` and `checksum_mode: when_required` for R2. It checks the
bucket with `HeadBucket`; whether R2 accepts that request from a token limited to one bucket
has not been verified. If setup reports access denied with a key that works elsewhere, try
again with a token that has an Admin permission for the setup only, then put the object token
into the config.

**5. Obsidian.** Create one more token per device (Object Read & Write for editing, Object Read
only for viewing), limited to the same bucket. `a2kb connect obsidian notes` prints the
endpoint, `auto` region, bucket, path-style URLs and prefix. See [obsidian](obsidian.md).

**6. First page.** Continue with the [first-run tutorial](first-run.md).

## Field mapping

How each value travels from the provider's console to the wizard, the config and the
Remotely Save plugin. The KB prefix is not a provider value: it is `kbs/<name>/` unless you
pass `--prefix`.

| Provider field | Wizard question (flag) | Config field | Remotely Save field |
|---|---|---|---|
| AWS region code, e.g. `eu-west-1` | "Region" (`--region`) | `storage.region` | Region; Endpoint `https://s3.<region>.amazonaws.com` |
| R2 account ID | "Cloudflare account id" (`--account-id`) | `storage.endpoint` = `https://<id>.r2.cloudflarestorage.com` | Endpoint |
| R2 jurisdictional endpoint | flag only (`--endpoint`) | `storage.endpoint` | Endpoint |
| (R2 has no region) | not asked | `storage.region: auto` | Region `auto` |
| Bucket name | "Bucket name" (`--bucket`) | `storage.bucket` | Bucket Name |
| Access key ID (AWS IAM user; R2 token's Access Key ID) | "Access key id" (`--access-key`, `KB_ACCESS_KEY`) | `storage.access_key`, or `kbs.<kb>.storage.access_key` | Access Key ID (a separate device key) |
| Secret access key | "Secret access key" (`--secret-key`, `KB_SECRET_KEY`) | `storage.secret_key`, or `kbs.<kb>.storage.secret_key` | Secret Access Key |
| AWS profile, role or SSO login | "Use the AWS credential chain ...?" (`--aws-chain`) | no keys; optional `storage.aws_profile` | not transferable: create a device key |
| (none) | `--kb-name`, `--prefix` | `kbs.<kb>`, `kbs.<kb>.prefix` | Change The Remote Prefix |
| (none) | preset | AWS: no `endpoint`; others: `endpoint` | S3 URL style: default on AWS, Path Style otherwise |

## Backblaze B2

```yaml
version: 2
storage:
  endpoint: https://s3.us-west-004.backblazeb2.com
  region: us-west-004
  access_key: <application key id>
  secret_key: <application key>
  bucket: kb-notes
  checksum_mode: when_required
kbs:
  notes: {}
```

Create one **application key per KB** (B2's name for an access key pair), restricted to the
bucket and to `namePrefix` `kbs/notes/` (a B2 setting that limits the key to object names
starting with that text), and put it in that KB's own `storage:` block. B2 then enforces the
prefix server-side.

## MinIO

```yaml
version: 2
storage:
  endpoint: http://minio.internal:9000
  access_key: <user access key>
  secret_key: <user secret key>
  bucket: kb
  addressing_style: path
  checksum_mode: when_required
kbs:
  notes: {}
```

Isolate KBs with a MinIO policy per user: allow `s3:*` on `arn:aws:s3:::kb/kbs/notes/*` and
`s3:ListBucket` on `arn:aws:s3:::kb` with a `s3:prefix` condition.

## SeaweedFS

```yaml
version: 2
storage:
  endpoint: http://seaweedfs:8333
  access_key: <identity access key>
  secret_key: <identity secret key>
  bucket: kb-bucket
kbs:
  notes: {}
```

Uploads work with both the default and `checksum_mode: when_required` (both tested), so no
setting is needed.

Identities can be scoped to a prefix (`Read:kb-bucket/kbs/notes/*`, ...). The
[Docker backend](docker.md) (`a2kb bootstrap`) generates one `<kb>-writer` and one
`<kb>-obsidian` identity per KB for you. SeaweedFS creates a bucket implicitly on the first
write, so a missing bucket may go unnoticed until then.

> **SeaweedFS older than 4.34 is affected by CVE-2026-55874; upgrade before relying on per-KB
> keys.** Its S3 gateway did not authorise the source of `CopyObject` / `UploadPartCopy`, so a
> key limited to one prefix could copy objects from another prefix or another bucket into its
> own prefix and read them (reproduced on 4.18). 4.48, which the Docker backend pins, refuses
> those requests (request matrix in [docker.md](docker.md#security-notes)).

What a prefix-scoped identity can and cannot do (SeaweedFS 4.18 and 4.48, tested): read, write,
list with a prefix, multipart upload and single deletes inside the prefix work; `HeadBucket`, a
listing without prefix and batch `DeleteObjects` are refused. `-s3.config` is read only at
start; see [docker.md](docker.md#seaweedfs-and-new-identities) for adding identities.

## Wasabi (untested)

```yaml
version: 2
storage:
  endpoint: https://s3.eu-central-1.wasabisys.com
  region: eu-central-1
  access_key: <access key>
  secret_key: <secret key>
  bucket: kb-notes
  checksum_mode: when_required
kbs:
  notes: {}
```

Wasabi supports IAM-style policies, so prefix-scoped sub-users should work like AWS.

## Isolation between KBs

agent2kb checks the prefix itself (`s3_client.py`), but that only protects against mistakes in
agent2kb. Real isolation needs the provider to enforce it too.

| Provider | Credential scope | Recommended layout |
|---|---|---|
| AWS S3 | IAM policy, prefix-level | One bucket, one prefix per KB, one IAM user/role per KB |
| Cloudflare R2 | API token, **bucket-level only** | One bucket per KB |
| Backblaze B2 | Application key, bucket + `namePrefix` | One bucket, one application key per KB |
| MinIO | Policy per user, prefix-level | One bucket, one user per KB |
| SeaweedFS | Identity actions, prefix-level | One bucket, one identity per KB |
| Wasabi | IAM-style policy, prefix-level | One bucket, one sub-user per KB |

An admin KB (`role: admin`) needs a key for the whole bucket and is the only one allowed to
touch `_meta/`. Keep that key out of project configs.

## Common errors

| Message | Likely cause |
|---|---|
| `the secret key does not match the access key` | `secret_key` is wrong for that `access_key` (or the key belongs to a different endpoint) |
| `the access key is not recognised` | `access_key` is wrong or from another account/endpoint |
| `Access denied (403)` | Wrong key, or key scope does not cover this bucket/prefix |
| `Bucket '...' does not exist` | Create it (`a2kb doctor --fix`) or fix `bucket:` |
| `Invalid bucket name` | Not 3-63 chars lowercase |
| `Cannot reach the S3 endpoint` | Wrong URL/port, server down, firewall |
| `different region` | Set `region:` to the bucket's region (`auto` for R2) |
| `checksum incompatibility` | Add `checksum_mode: when_required` |

Put `-v` before the command (`a2kb -v status`) to see the full traceback. `a2kb -v doctor` also
shows the underlying error code and traceback for each failed check.

## Legacy profiles

Config files in the old formats keep working unchanged: a flat `.kb/config.yaml` for one
project, or a `profiles:` map in `~/.kb/config.yaml`. They use the older field names
(`s3_endpoint` for `endpoint`) and an explicit `prefix`:

```yaml
profiles:
  notes:
    s3_endpoint: http://seaweedfs:8333
    access_key: <key>
    secret_key: <secret>
    bucket: kb-bucket
    prefix: projects/notes/
```

Everything above applies to them with `s3_endpoint` for `endpoint`, and with the profile's
`prefix` in place of `kbs/<name>/` in policies. The profile named `super` is the old form of
an admin KB and is never selected automatically. `a2kb config migrate` converts a profile to
v2 without moving any object; see [configuration](configuration.md#legacy-v1-configs).
