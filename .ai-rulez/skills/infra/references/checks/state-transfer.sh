#!/bin/sh
# Check public HUMAN transfer evidence, never pull or display secret-bearing
# state. HCP detection is conservative and textual: HCL comments are stripped
# before inspection and Terraform JSON is parsed structurally, so neither
# comments between tokens nor escape-encoded JSON keys can make a previously
# HCP-routed leaf look genuinely new.
set -eu
cannot_verify() { echo "CANNOT VERIFY state transfer: $1" >&2; exit 2; }
incomplete() { echo "INCOMPLETE state transfer: $1" >&2; exit 1; }
leaf=${1:-}
case "$leaf" in ''|*[!a-z0-9-]*) cannot_verify 'a declared leaf slug is required' ;; esac

# Flatten HCL to one comment-free line, so tokens that only become adjacent
# after removing HCL comments are still detected. Bias: stripping comment text
# can only create false evidence requirements, never hide real tokens. Strings
# are not tracked; this is not a general parser.
flatten_hcl() {
    awk '
        BEGIN { inside_block = 0; whole = "" }
        {
            line = $0
            stripped = ""
            while (1) {
                if (inside_block) {
                    closer = index(line, "*/")
                    if (closer == 0) { line = ""; break }
                    line = substr(line, closer + 2)
                    inside_block = 0
                    continue
                }
                hash = index(line, "#")
                slash = index(line, "//")
                block = index(line, "/*")
                if (hash == 0) hash = 999999999
                if (slash == 0) slash = 999999999
                if (block == 0) block = 999999999
                if (hash == 999999999 && slash == 999999999 && block == 999999999) {
                    stripped = stripped line
                    break
                }
                if (hash < slash && hash < block) { stripped = stripped substr(line, 1, hash - 1); break }
                if (slash < block) { stripped = stripped substr(line, 1, slash - 1); break }
                stripped = stripped substr(line, 1, block - 1)
                rest = substr(line, block + 2)
                closer = index(rest, "*/")
                if (closer == 0) { inside_block = 1; break }
                line = substr(rest, closer + 2)
            }
            if (stripped != "") whole = whole " " stripped
        }
        END { print whole }
    '
}

hcl_hcp() {
    printf '%s\n' "$1" |
        grep -Eq '(^|[[:space:]{}])backend[[:space:]]*"remote"|(^|[[:space:]{}])cloud[[:space:]]*\{'
}

# History asks only whether an old revision configured HCP (cloud block or the
# remote backend). The current-tree layout check instead rejects every backend
# or cloud declaration outside the reviewed backend.tf.
HISTORY_FILTER='any((.terraform // {}) | .. | objects;
  has("cloud") or ((.backend // {}) | type == "object" and has("remote")))'
LAYOUT_FILTER='any((.terraform // {}) | .. | objects; has("cloud") or has("backend"))'

json_check() {
    status=0
    verdict=$(printf '%s\n' "$1" | jq -er "$2" 2>/dev/null) || status=$?
    case "$status" in
        0) printf '%s\n' "$verdict" ;;
        1) [ "$verdict" = false ] || cannot_verify "$3"
           printf '%s\n' false ;;
        *) cannot_verify "$3" ;;
    esac
}

# Inspect one revision's top-level leaf files for committed HCP configuration;
# sets `found` to hcp/true/false.
inspect_revision() {
    revision=$1
    files=$(git --no-optional-locks ls-tree -r --name-only "$revision" -- "terraform/$leaf/") || {
        cannot_verify "cannot list $revision"
    }
    found=false
    while IFS= read -r file; do
        depth="${file#terraform/$leaf/}"
        [ "$depth" != "$file" ] || continue
        case "$depth" in
            (*/*) continue ;;
            (*.tf|*.tf.json) : ;;
            (*) continue ;;
        esac
        snapshot=$(git --no-optional-locks show "$revision:$file") || {
            cannot_verify "cannot read $file at $revision"
        }
        case "$depth" in
            (*.tf.json)
                found=$(json_check "$snapshot" "$HISTORY_FILTER" \
                    "cannot structurally inspect $file at $revision") || exit 2
                [ "$found" = true ] && found=hcp || found=false
                ;;
            (*.tf)
                if hcl_hcp "$(printf '%s\n' "$snapshot" | flatten_hcl)"; then
                    found=hcp
                else
                    found=false
                fi
                ;;
        esac
        [ "$found" = false ] || break
    done <<FILES
$files
FILES
}

# The leaf's backend declaration at a revision, comment- and whitespace-free, so
# moving the same block between files compares equal and any retarget does not.
# Prints nothing if a .tf.json declares a backend: that form is not compared.
backend_declaration() {
    git --no-optional-locks ls-tree -r --name-only "$1" -- "terraform/$leaf/" |
    while IFS= read -r file; do
        depth="${file#terraform/$leaf/}"
        case "$depth" in
            (*/*) continue ;;
            (*.tf.json)
                verdict=$(json_check "$(git --no-optional-locks show "$1:$file")" \
                    "$LAYOUT_FILTER" "cannot structurally inspect $file at $1") || exit 2
                [ "$verdict" = false ] || echo 'json-backend' ;;
            (*.tf)
                snapshot=$(git --no-optional-locks show "$1:$file") || exit 2
                # Cheap pre-filter: only a file with a backend label can hold one.
                printf '%s\n' "$snapshot" | LC_ALL=C grep -Eq 'backend[[:space:]]*"' || continue
                printf '%s\n' "$snapshot" | backend_blocks ;;
        esac
    done
}

# Print each `backend "<type>" { ... }` block of one HCL file on its own line.
# String-aware, unlike flatten_hcl: `//`, `#` and braces inside quoted strings
# (an https endpoint, say) are kept, comments outside strings are dropped, and
# nested blocks are matched to the real closing brace. Whitespace outside
# strings is removed, so the same block compares equal wherever it lives.
backend_blocks() {
    LC_ALL=C awk '
        { text = text $0 "\n" }
        END {
            n = length(text); out = ""; i = 1
            while (i <= n) {
                c = substr(text, i, 1); two = substr(text, i, 2)
                if (c == "\"") {
                    j = i + 1
                    while (j <= n) {
                        d = substr(text, j, 1)
                        if (d == "\\") { j += 2; continue }
                        if (d == "\"" || d == "\n") break
                        j++
                    }
                    out = out substr(text, i, j - i + 1); i = j + 1; continue
                }
                if (c == "#" || two == "//") { while (i <= n && substr(text, i, 1) != "\n") i++; continue }
                if (two == "/*") { j = index(substr(text, i + 2), "*/"); i = (j ? i + j + 3 : n + 1); continue }
                if (c ~ /[[:space:]]/) { if (out ~ /[A-Za-z0-9_]$/) pending = 1; i++; continue }
                if (pending && c ~ /[A-Za-z0-9_]/) out = out " "
                pending = 0; out = out c; i++
            }
            m = length(out); k = 1
            while (k <= m) {
                c = substr(out, k, 1)
                if (c == "\"") {
                    k++
                    while (k <= m && substr(out, k, 1) != "\"") k += (substr(out, k, 1) == "\\") ? 2 : 1
                    k++; continue
                }
                if (substr(out, k, 8) == "backend\"" && (k == 1 || substr(out, k - 1, 1) !~ /[A-Za-z0-9_]/)) {
                    start = k; depth = 0; k += 7; instr = 0
                    while (k <= m) {
                        c = substr(out, k, 1)
                        if (instr) { if (c == "\\") k++; else if (c == "\"") instr = 0 }
                        else if (c == "\"") instr = 1
                        else if (c == "{") depth++
                        else if (c == "}") { depth--; if (depth == 0) break }
                        k++
                    }
                    print substr(out, start, k - start + 1)
                }
                k++
            }
        }'
}

history=''
revisions=$(git --no-optional-locks log --format=%H HEAD -- "terraform/$leaf/") || {
    cannot_verify 'cannot read backend history'
}
for revision in $revisions; do
    inspect_revision "$revision"
    [ "$found" = false ] || history=$revision
    [ "$history" = '' ] || break
done
if [ "$history" = '' ]; then
    shallow=$(git --no-optional-locks rev-parse --is-shallow-repository) || exit 2
    [ "$shallow" = false ] || cannot_verify 'fetch complete history before ruling out an HCP cutover'
    exit 0
fi

# The attestation hashes backend.tf alone, so it must hold the leaf's only
# backend declaration, with no competing backend/cloud syntax anywhere else.
backend_file="terraform/$leaf/backend.tf"
[ -f "$backend_file" ] || incomplete "declare the $leaf backend in $backend_file"
{
    printf '%s\n' "$backend_file"
    ls "terraform/$leaf/"*.tf "terraform/$leaf/"*.tf.json 2>/dev/null | sort -u || true
} | awk '!seen[$0]++' | while IFS= read -r source_file; do
    case "$source_file" in
        (*.tf|*.tf.json) : ;;
        (*) continue ;;
    esac
    if [ "$source_file" = "${source_file%.tf.json}" ]; then
        flattened=$(flatten_hcl < "$source_file")
        declarations=$(printf '%s\n' "$flattened" |
            grep -oE '(^|[[:space:]{}])backend[[:space:]]*"[^"]+"' | wc -l | tr -d '[:space:]')
        cloud_declarations=$(printf '%s\n' "$flattened" |
            grep -cE '(^|[[:space:]{}])cloud[[:space:]]*\{' || true)
    else
        verdict=$(json_check "$(cat "$source_file")" "$LAYOUT_FILTER" \
            "cannot structurally inspect $source_file") || exit 2
        if [ "$verdict" = true ]; then
            declarations=1
            cloud_declarations=1
        else
            declarations=0
            cloud_declarations=0
        fi
    fi
    if [ "$source_file" = "$backend_file" ]; then
        [ "$declarations" = 1 ] && [ "$cloud_declarations" = 0 ] ||
            incomplete 'put the single effective backend block in backend.tf'
    else
        [ "$declarations" = 0 ] && [ "$cloud_declarations" = 0 ] ||
            incomplete "backend or cloud declaration outside reviewed backend.tf: $source_file"
    fi
done
git ls-files --error-unmatch "$backend_file" README.md >/dev/null 2>&1 ||
    incomplete 'commit the destination backend and config'
git diff --quiet HEAD -- "$backend_file" README.md || exit 1
git diff --cached --quiet HEAD -- "$backend_file" README.md || exit 1
blob=$(git hash-object "$backend_file") || exit 2
transfers=${STATE_TRANSFERS-'{}'}

# A cutover made before this evidence existed cannot be re-attested: the HCP
# workspace and the frozen snapshot may be gone. A legacy record names the
# cutover instead: the first-parent commit of HEAD that left HCP, so a merge or
# squash counts from when it landed, not from its branch commits' own dates.
# It is accepted only once that commit is legacy_min_age_days old, so a fresh
# cutover cannot use it to skip the frozen-transfer evidence. A committer date
# is still forgeable by anyone who can push directly; this stops accidents and
# fresh migrations, not a determined admin.
legacy_min_age_days=30
if printf '%s' "$transfers" | jq -e --arg leaf "$leaf" '.[$leaf].legacy == true' >/dev/null 2>&1; then
    cutover=''
    newer=''
    mainline=$(git --no-optional-locks log --first-parent --format=%H HEAD -- "terraform/$leaf/") || {
        cannot_verify 'cannot read first-parent backend history'
    }
    for revision in $mainline; do
        inspect_revision "$revision"
        [ "$found" = false ] || { cutover=$newer; break; }
        newer=$revision
    done
    [ "$cutover" != '' ] ||
        incomplete "legacy record for $leaf, but no first-parent commit of HEAD left HCP; remove legacy and record the frozen transfer"
    cutover_epoch=$(git --no-optional-locks log -1 --format=%ct "$cutover") || exit 2
    now_epoch=$(date +%s)
    printf '%s' "$transfers" | jq -e --arg leaf "$leaf" --arg blob "$blob" --arg cutover "$cutover" '
      .[$leaf] as $transfer
      | $transfer.backend_blob == $blob
        and $transfer.cutover_commit == $cutover
        and ($transfer.migration | type == "string" and test("\\Ahttps://[^[:space:]]+\\z"))
    ' >/dev/null || incomplete "legacy record for $leaf must name cutover_commit $cutover, the current backend_blob and a migration URL"
    then_backend=$(backend_declaration "$cutover") || exit 2
    now_backend=$(backend_declaration HEAD) || exit 2
    case "$then_backend" in ''|*json-backend*)
        incomplete "legacy record for $leaf needs an HCL backend block at cutover $cutover; record the frozen transfer" ;;
    esac
    [ "$then_backend" = "$now_backend" ] ||
        incomplete "legacy record for $leaf: the backend changed after cutover $cutover; remove legacy and record the frozen transfer"
    [ $((now_epoch - cutover_epoch)) -ge $((legacy_min_age_days * 86400)) ] ||
        incomplete "legacy record for $leaf: cutover $cutover landed less than $legacy_min_age_days days ago; remove legacy and record the frozen transfer"
    echo "LEGACY state transfer for $leaf: cutover $cutover predates transfer evidence, not re-verified" >&2
    exit 0
fi
printf '%s' "$transfers" | jq -e --arg leaf "$leaf" --arg blob "$blob" '
  .[$leaf] as $transfer
  | ($transfer | type == "object")
    and $transfer.source_backend == "hcp"
    and $transfer.backend_blob == $blob
    and ($transfer.workspace_id | type == "string" and test("^ws-[A-Za-z0-9]+$"))
    and ($transfer.serial | type == "number" and . >= 0 and . == floor)
    and ($transfer.lineage | type == "string" and length > 0)
    and ($transfer.state_sha256 | type == "string" and test("^[a-f0-9]{64}$"))
    and $transfer.hcp_locked_before_pull == true
    and $transfer.destination_verified == true
    and ($transfer.verified_at | type == "string" and
      test("^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$") and
      (. as $raw | try strptime("%Y-%m-%dT%H:%M:%SZ") catch null
       | select(. != null) | mktime as $epoch
       | ($epoch | strftime("%Y-%m-%dT%H:%M:%SZ")) == $raw and $epoch <= now))
' >/dev/null || incomplete "record the frozen, verified HUMAN HCP transfer for $leaf"
