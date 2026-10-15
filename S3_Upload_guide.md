# Uploading the OCR output to S3 (macOS, Apple silicon)

Steps from installing `gobbc` to uploading the `out_summary/*.xlsx` files. Written from a real setup on an Apple-silicon Mac;
the `gobbc` login step depends on your team's own documentation (marked below).

Bucket used in this project: `vjsthasia-sir-final-roll-up`, region `eu-west-1`, prefix `sir-final-roll/summary/`.
Replace these with your own values where needed.

---

## 1. Check you can reach GitHub (optional sanity check)

```bash
GIT_TERMINAL_PROMPT=0 git ls-remote --refs --tags https://github.com/bbc/gobbc v1.27.0
```

A commit hash plus `refs/tags/v1.27.0` means git can read the repo. This does **not** prove AWS access.

## 2. Install `gobbc`

### 2a. Homebrew (try this first, but see the known problems)

```bash
brew tap bbc/gobbc https://github.com/bbc/gobbc
brew trust bbc/gobbc
brew install gobbc
```

Known problems seen with this route:

| Error | Cause | What to do |
|---|---|---|
| `could not read Username for 'https://github.com': terminal prompts disabled` | Homebrew clones the repo in a sandbox with no way to ask for a login, and its environment is stripped (environment variables you set are not passed) | Pre-clone the repo into the Homebrew cache: `rm -rf ~/Library/Caches/Homebrew/gobbc--git && git clone https://github.com/bbc/gobbc ~/Library/Caches/Homebrew/gobbc--git`, then `brew install gobbc` again |
| `Errno::ENOENT … files/dist/gobbc_darwin_arm64_v8.0/gobbc` | The formula expects a prebuilt binary that is missing from the repo at v1.27.0 | Use the manual install in 2b, and report the missing file to the `gobbc` maintainers |

### 2b. Manual install from the GitHub release (worked)

1. Open `https://github.com/bbc/gobbc/releases` (logged in to GitHub).
2. Download the macOS arm64 package, for example `gobbc_1.27.0_darwin_arm64`, and unpack it (it is in `~/Downloads` in this example).
3. Make the binary runnable and clear the macOS download flag:

```bash
ls ~/Downloads/gobbc_1.27.0_darwin_arm64                       # you should see a file called gobbc
chmod +x ~/Downloads/gobbc_1.27.0_darwin_arm64/gobbc
xattr -d com.apple.quarantine ~/Downloads/gobbc_1.27.0_darwin_arm64/gobbc    # "No such xattr" is fine
```

4. Put it on your PATH (no admin rights needed):

```bash
mkdir -p ~/.local/bin
cp ~/Downloads/gobbc_1.27.0_darwin_arm64/gobbc ~/.local/bin/
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

5. Check:

```bash
which gobbc
gobbc --help
```

If macOS says the app is from an unidentified developer: System Settings → Privacy & Security → **Allow Anyway**, then run it again.

Copying the file into `~/.local/bin` is the whole install; do not run `brew install gobbc` afterwards. To update later, download the new
release and repeat step 4.

## 3. Install the AWS CLI

```bash
aws --version || brew install awscli
```

## 4. Get temporary AWS access

Use `gobbc` as your team documents (login / get-credentials command; run `gobbc --help` to see what is available).

`gobbc aws-credentials -account <account-id-from-previous-step>`

Do not paste keys or tokens into chat, tickets or code.

## 5. Check access to the bucket

```bash
aws s3 ls s3://vjsthasia-sir-final-roll-up/ --region eu-west-1
```

A listing (or no output for an empty bucket) means it works. `AccessDenied` means the credentials from step 4 do not cover this bucket, or have
expired: repeat step 4, or ask the bucket owner.

## 6. Upload

### 6a. Test first with `--dryrun`

```bash
aws s3 cp out_summary/ \
  s3://vjsthasia-sir-final-roll-up/sir-final-roll/summary/ \
  --recursive \
  --exclude "*" --include "*.xlsx" \
  --region eu-west-1 \
  --dryrun
```

`--dryrun` only **lists** what would be uploaded (lines start with `(dryrun)`). Nothing is stored, so the bucket stays empty.
`--exclude "*"` must come before `--include`, otherwise every file is selected.

### 6b. Real upload

Run the same command **without** `--dryrun`.

### 6c. Adding more files later (keeps what is already there)

```bash
aws s3 sync out_summary/ \
  s3://vjsthasia-sir-final-roll-up/sir-final-roll/summary/ \
  --exclude "*" --include "*.xlsx" \
  --region eu-west-1
```

`sync` uploads only new or changed files and leaves everything else in the bucket alone. Do **not** add `--delete`: it removes bucket files that are not in
your local folder. A local file with the same name as one in the bucket (for example a re-run `10_summary.xlsx`) overwrites it; turn on bucket
versioning in the AWS console if old copies must be kept.

S3 has no real folders: `sir-final-roll/summary/` shows in the console once at least one file is stored under that path.

### 6d. Other outputs

```bash
# card OCR output, without the temporary _parts folders
aws s3 sync out_deleted/ s3://vjsthasia-sir-final-roll-up/sir-final-roll/cards/ \
  --exclude "_parts/*" --region eu-west-1 --dryrun
```

The card script can also upload each finished AC folder itself: `--upload s3://vjsthasia-sir-final-roll-up/sir-final-roll/cards`
(and `--presign-hours N` for share links). Test it with one AC before a big run.

## 7. Sharing a file

The plain object URL (`https://<bucket>.s3.<region>.amazonaws.com/<key>`) returns **Access Denied**, because buckets are private by default.

Temporary share link (works for people without AWS accounts):

```bash
aws s3 presign s3://vjsthasia-sir-final-roll-up/sir-final-roll/summary/10_summary.xlsx \
  --expires-in 604800 --region eu-west-1
```

* 604800 seconds = 7 days, the maximum.
* A link created with **temporary credentials** (such as those from `gobbc`) stops working when that session expires, which can be much sooner
  than 7 days. Re-create links when needed.
* Console alternative: select the file → **Object actions → Share with a presigned URL**.

Making files public (turning off Block Public Access plus a bucket policy) is usually against organisation policy: ask the bucket owner first.
Giving named colleagues AWS access to the bucket is the safer permanent option.

## Quick reference

| Task | Command |
|---|---|
| Check access | `aws s3 ls s3://vjsthasia-sir-final-roll-up/ --region eu-west-1` |
| Test upload | `aws s3 cp out_summary/ s3://…/sir-final-roll/summary/ --recursive --exclude "*" --include "*.xlsx" --region eu-west-1 --dryrun` |
| Upload / add more | `aws s3 sync out_summary/ s3://…/sir-final-roll/summary/ --exclude "*" --include "*.xlsx" --region eu-west-1` |
| Share link | `aws s3 presign s3://…/<file> --expires-in 604800 --region eu-west-1` |