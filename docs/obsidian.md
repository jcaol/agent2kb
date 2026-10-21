# Reading and editing a KB in Obsidian

> agent2kb is an independent project, not affiliated with or endorsed by Obsidian or the
> authors of Remotely Save. Their names are used only to describe compatibility.

People read a KB in [Obsidian](https://obsidian.md) through the community plugin
**Remotely Save**, which synchronises a vault with an S3-compatible bucket. agent2kb itself has
no Obsidian component: the plugin and agent2kb meet only in the bucket, under the KB's prefix,
as plain Markdown files. This page applies to every storage setup: the Docker backend, AWS S3,
Cloudflare R2 and the other providers.

> **What has been verified.** The field names and the prefix handling below were read from the
> plugin's source (release 0.5.25 at the time) and the S3 calls it makes were replayed against
> SeaweedFS ([docker-testing.md](docker-testing.md)). The maintainer has used Remotely Save with
> Obsidian on several desktop and mobile devices against a self-hosted SeaweedFS backend with
> earlier agent2kb versions; **the recipes on this page have not yet been re-run end to end for
> the current release**, nor against AWS or R2. A newer plugin version may label its settings
> differently.
> Reports are welcome.

## 1. Get the settings sheet

```bash
a2kb connect obsidian notes                  # everything except the secret
a2kb connect obsidian notes --show-secret    # including the secret (keep it out of logs)
```

With the Docker backend run the same command inside the `kb` service
(`docker compose exec kb a2kb connect obsidian notes --show-secret`). The sheet lists the
endpoint, region, bucket, URL style, the **remote prefix** and the access key, with the plugin's
own field labels, followed by the steps below.

Which key the sheet shows:

| Setup | Access key on the sheet |
|---|---|
| Docker backend | The KB's own `<kb>-obsidian` key, limited to `kbs/<kb>/` (generated for you) |
| A KB with static keys in its config (R2, B2, MinIO, AWS with keys, ...) | The **same key agent2kb uses**, with a warning. Prefer a separate key for each device, see [Which key to give the plugin](#2-which-key-to-give-the-plugin) |
| A KB that uses the AWS credential chain (no keys in the config) | **Placeholders**: the sheet shows endpoint, region, bucket and prefix, and you enter a separate key that you create for Obsidian |

Never copy a machine's AWS login (an SSO session, an assumed role, the values in
`~/.aws/credentials` that agent2kb uses) into the plugin. Those are the machine's credentials,
often temporary, and usually broader than one KB. Create a key for the device instead, so that
you can revoke it alone when the device is lost.

## 2. Which key to give the plugin

Choose one of the two recipes. Both use a key that is limited to the KB (to its bucket on R2,
to its prefix where the provider supports it; see
[isolation](storage-providers.md#isolation-between-kbs)).

### Recipe A: viewing only (pull)

For people who only read the compiled pages.

- **Key permissions:** list and read objects only. On AWS: `s3:ListBucket` (with an `s3:prefix`
  condition) and `s3:GetObject` on the KB's prefix. On R2: an API token with **Object Read
  only** for the KB's bucket. With the Docker backend there is no read-only Obsidian key; use
  recipe B.
- **Plugin:** set the sync direction to pull only. The plugin's source defines the directions
  `bidirectional`, `incremental_pull_only`, `incremental_push_only`,
  `incremental_pull_and_delete_only` and `incremental_push_and_delete_only`; the wording in the
  settings page may differ.
- **Not verified:** whether the plugin writes any bookkeeping object to the bucket even in a
  pull-only mode. If a sync with a read-only key fails with "access denied", that is the likely
  cause; use recipe B instead.
- Notes and Web Clipper files that you create in this vault stay on the device; they never reach
  the KB.

### Recipe B: editable, with Web Clipper

The normal setup for people who also add notes, edit pages or use the Obsidian Web Clipper.

- **Key permissions:** list, read, write and **delete** objects in the KB's prefix. Two-way
  synchronisation uploads and deletes objects, so a read-only key cannot support it. On AWS:
  the runtime policy in [storage-providers.md](storage-providers.md#aws-s3), attached to a
  separate IAM user for the device. On R2: a separate API token with **Object Read & Write**
  for the KB's bucket. With the Docker backend: the generated `<kb>-obsidian` key.
- **Plugin:** the bidirectional sync direction.
- Only files under `Clippings/` (or `wiki/Clippings/`) become sources: compile imports them
  into `raw/`. A note saved elsewhere in the vault is uploaded and kept in the bucket, but
  compile does not read it.
- Give every device, or at least every person, its own key, so that one can be revoked without
  touching agent2kb or the other devices. Revoke it at the provider (AWS: deactivate or delete
  the IAM access key; R2: delete the API token; Docker:
  `a2kb bootstrap rotate <kb> --role obsidian`, see [docker.md](docker.md#rotating-or-revoking-keys)).

## 3. Configure the plugin

1. Create a **new, empty vault** for the KB. The first sync downloads the whole KB into it;
   an existing vault would upload its own notes into the KB. Back up any vault before you point
   a sync plugin at it (the plugin's own README says the same).
2. Install and enable the community plugin "Remotely Save". Choose the remote service
   "S3 or compatible" and copy the values from the sheet.
3. Type the remote prefix from the sheet (for example `kbs/notes/`) into
   "Change The Remote Prefix" and press Confirm. Remotely Save has no folder setting for S3:
   with the prefix, the vault root **is** the KB's prefix. Without it the vault would be the
   whole bucket, which a key limited to one KB cannot list.
4. **Leave the encryption password empty** for this prefix. See [Encryption](#encryption).
5. Leave "sync config folder" off, unless you want `.obsidian/` uploaded into the KB as well.
6. Do not rely on the connectivity "Check": it lists the whole bucket, which a key limited to
   one prefix is not allowed to do, so it can report a failure although syncing works. Run a
   manual sync instead (ribbon icon or command palette).
7. Keep automatic sync off until the check in [section 6](#6-first-check-sync-compile-sync)
   has passed.

**CORS.** The sheet says to keep "Bypass CORS Issue Locally" on, which is the plugin's default.
Do not make the bucket public and do not add a permissive CORS rule to the bucket as a matter
of course. Configure CORS at the provider only if a sync fails with a CORS error and the bypass
does not help on your platform; the plugin's README notes that very old Obsidian versions
needed it.

**Phones.** A phone cannot reach `127.0.0.1` or a name that only resolves inside Docker, and
Obsidian on iOS refuses plain `http://` in practice. Use an HTTPS endpoint; for the Docker
backend see [docker.md](docker.md#6-https-for-phones-and-remote-agents-optional).

## 4. What is in the vault

| Folder | Written by | Notes |
|---|---|---|
| `raw/` | `a2kb ingest`, compile (imported clippings) | The sources, with YAML frontmatter. Raw material; edit only to correct a source. |
| `wiki/` | `a2kb compile` | The generated pages, one per raw file with the same file name, plus `index.md`, `log.md` and `concepts/`. |
| `wiki/topics/` | `a2kb promote --as wiki-topic` | Pinned pages that compile never regenerates. |
| `outputs/` | `a2kb query` (saved answers), `a2kb promote` | Fed back into the next compile as derived context. Agent answers go to `outputs/agent/`. |
| `Clippings/`, `wiki/Clippings/` | You, the Obsidian Web Clipper | Imported into `raw/` by the next compile (or `a2kb clip`) and then removed there. |

**When a page is regenerated.** Compile rewrites `wiki/<file>.md` when its raw file is new or
has changed, or with `a2kb compile --force`. A hand edit in a generated page survives until
then and is lost at the next regeneration. Put text that must stay in a raw source (edit or
re-ingest it), or promote it as a pinned topic (`a2kb promote --as wiki-topic`).
Unresolved `[[wikilinks]]` are intentional ghost nodes; `a2kb stubs` creates placeholder pages
for the most frequently referenced ones, and `assets/obsidian-snippets/dim-ghost-links.css`
dims them.

**Clippings.** A clipping that compile has imported is deleted from `Clippings/` in the bucket.
The device that clipped it still has the file and, depending on the plugin's sync state, may
upload it again; compile recognises such a duplicate (by content hash and source URL) and
discards it instead of importing it twice.

## 5. Two clocks: compile and sync

Compile and sync run independently:

- **Compile** runs when you call `a2kb compile`, or on the server's schedule (the Docker backend
  compiles every 15 minutes; on your own server, `scripts/kb-crontab.example`). It runs whether
  or not Obsidian is open.
- **Sync** runs only while Obsidian is open on the device: manually, every N minutes, or on save,
  as configured in the plugin. The plugin's README states that automatic sync does not run while
  Obsidian is in the background.

A newly ingested source therefore appears in the vault only after both have happened: compile
on the server, then a sync on the device. A note or clipping written on the device reaches the
KB at the device's next sync and becomes a wiki page at the next compile after that.

## 6. First check: sync, compile, sync

Before you turn on automatic sync:

1. In the empty vault, run a manual sync. Expected: the folders `raw/`, `wiki/` and `outputs/`
   appear, with `wiki/index.md` and `wiki/log.md`.
2. On the machine with agent2kb, ingest something and compile it, for example the sample in the
   [first-run tutorial](first-run.md):
   `a2kb -p notes ingest --file examples/first-note.md`, then `a2kb -p notes compile`.
3. Sync again. Expected: the new page in `wiki/` (same file name as the raw file) and a new
   entry in `wiki/log.md`.
4. For recipe B only: create a short note in the vault's `Clippings/` folder, sync, and run
   `a2kb -p notes compile`. Expected: the compile output says
   `Imported 1 Web Clipper clipping(s) to raw/`, and after the next sync the note has left
   `Clippings/` and a page for it is in `wiki/`.

Then enable automatic sync if you want it.

## Encryption

Two different kinds of encryption apply here:

- **Client-side encryption in the plugin.** When a password is set, Remotely Save encrypts the
  files before they are uploaded (its README names the openssl and rclone crypt formats);
  without a password it uploads the original content. agent2kb has no way to decrypt those
  files: compile, query, MCP and backup read the objects as plain UTF-8 Markdown. **For the
  KB's prefix, leave the plugin's encryption password empty.** Setting one would make every
  file the device uploads unreadable to agent2kb, and the plugin cannot decrypt the plain files
  that agent2kb writes. (This is inferred from both implementations; it has not been tried on
  a device.)
- **Encryption at rest by the storage provider.** Server-side encryption (for example AWS S3's
  default encryption, or the encryption a provider applies to stored data) is transparent to
  both agent2kb and the plugin, and needs no setting here.

If the notes must be protected from the storage provider itself, keep the KB on storage you
control (the Docker backend, MinIO) and use a local LLM; see [privacy](privacy.md).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Check" fails, but a sync works | Expected with a key limited to one prefix (step 6 above) |
| Sync fails with access denied | Remote prefix not set or not confirmed; the key covers another bucket or prefix; a read-only key used for two-way sync |
| The vault shows other KBs or the whole bucket | The remote prefix is empty; set it, then use a new vault |
| A page that compile reports is missing on the device | Obsidian was closed or no sync ran since the compile (section 5) |
| Files uploaded from the device look like random text in the bucket | The plugin's encryption password is set for this prefix |
| The phone cannot connect | `127.0.0.1`, an internal name or plain `http://`; see [Phones](#3-configure-the-plugin) |
