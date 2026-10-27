"""Публикации (выпуск, слияние, `gh api`, curl/wget/Invoke-WebRequest с записью), необратимые для запуска роли."""
import re
from dguard.words import cmd_name, PY_NAMES


PUBLISH_SUBS = {      # публикации, кроме `git push` (его разбирает git_scan): команда → подкоманды среди первых слов без опций
    "gh": (("release", "create"), ("release", "upload"), ("release", "edit"), ("pr", "merge")),
    "npm": (("publish",),), "pnpm": (("publish",),), "yarn": (("publish",),), "cargo": (("publish",),),
    "poetry": (("publish",),), "uv": (("publish",),), "twine": (("upload",),), "gem": (("push",),),
    "docker": (("push",), ("image", "push")),
}


WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
HTTP_NAMES = {"curl", "wget", "invoke-webrequest", "iwr", "invoke-restmethod", "irm"}
GITHUB_API_HOST = re.compile(                    # хост адреса, а не кусок пути: api.github.com, https://u@uploads.github.com:443/…
    r"(?:^|[=:'\"\s])(?:[a-z][a-z0-9+.-]*://)?(?:[^/\s@]*@)?((?:api|uploads)\.github\.com)(?=[:/?#'\"]|$)", re.I)
# адреса GitHub API, где запись — слияние или выпуск: слияние PR и веток, релизы и их файлы, ветки и теги, файлы репозитория
GH_API_PUBLISH = re.compile(r"(?:^|/)(?:pulls/[^/?]+/merge|merges|releases|git/refs|git/tags|contents)(?:[/?]|$)", re.I)
GH_GRAPHQL_PUBLISH = re.compile(r"\b(?:mergePullRequest|enablePullRequestAutoMerge|createRef|updateRef|deleteRef|"
                                r"createCommitOnBranch|createRelease)\b")
GH_API_VALUE = {"-X", "--method", "-H", "--header", "-f", "--raw-field", "-F", "--field", "--input", "-q", "--jq", "-t",
                "--template", "--hostname", "-p", "--preview", "--cache"}
GH_API_FIELD = {"-f", "--raw-field", "-F", "--field", "--input"}      # поле или тело: без -X gh api делает POST
CURL_VALUE_SHORT = set("AbcCDeEHKmoPQrtuUwxyYz")                      # короткие опции curl со значением (кроме d F T X)
CURL_BODY_LONG = ("--data", "--data-ascii", "--data-binary", "--data-raw", "--data-urlencode", "--json", "--form",
                  "--form-string", "--upload-file")
WGET_BODY_LONG = ("--post-data", "--post-file", "--body-data", "--body-file")


def gh_api_label(args):
    """`gh api <адрес>` с записью (`-X/--method POST|PUT|PATCH|DELETE` либо поля/тело `-f -F --field --input`, что без `-X`
    делает POST) на адрес слияния или выпуска (GH_API_PUBLISH) либо мутация GraphQL слияния/ветки/релиза → метка либо None.
    Чтение (`gh api` без записи, `-X GET` с полями — это запрос) свободно."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-R", "--repo") else 1
    if i >= len(args) or args[i] != "api":
        return None
    method, fields, endpoint, texts = None, False, None, []
    rest = args[i + 1:]
    j = 0
    while j < len(rest):
        a = rest[j]
        j += 1
        if a.startswith("--") and "=" in a:
            opt, _, val = a.partition("=")
        elif a.startswith("-") and not a.startswith("--") and len(a) > 2 and a[:2] in GH_API_VALUE:   # -XPOST, -fkey=v
            opt, val = a[:2], a[2:]
        elif a.startswith("-") and a != "-":
            opt, val = a, ""
            if opt in GH_API_VALUE and j < len(rest):
                val = rest[j]
                j += 1
        else:
            endpoint = endpoint if endpoint is not None else a
            continue
        if opt in ("-X", "--method"):
            method = val.strip().upper()
        elif opt in GH_API_FIELD:
            fields = True
            texts.append(val)
    if endpoint is None or not (method in WRITE_METHODS if method else fields):
        return None
    path = re.sub(r"^https?://[^/]+", "", endpoint.strip())
    if GH_API_PUBLISH.search(path) or (path.strip("/").lower() == "graphql" and any(GH_GRAPHQL_PUBLISH.search(t) for t in texts)):
        return f"gh api {method or 'POST'} {endpoint}"[:60]
    return None


def curl_writes(args):
    """curl пишет: `-X/--request POST|PUT|PATCH|DELETE`, без `-X` — `-d/--data*`, `--json`, `-F/--form*`, `-T/--upload-file`
    (кроме `-G/--get`: данные уходят в адрес запросом). Короткие опции склеены: `-sXPOST`, `-sd x`."""
    method, body, get = None, False, False
    j = 0
    while j < len(args):
        a = args[j]
        j += 1
        if a.startswith("--"):
            opt, eq, val = a.partition("=")
            low = opt.lower()
            if len(low) >= 5 and "--request".startswith(low):
                method = (val if eq else args[j] if j < len(args) else "").upper()
            elif low == "--get":
                get = True
            elif low in CURL_BODY_LONG or (len(low) >= 5 and any(w.startswith(low) for w in CURL_BODY_LONG)):
                body = True
        elif a.startswith("-") and a != "-":
            cluster = a[1:]
            for n, c in enumerate(cluster):
                if c in "dFT":
                    body = True
                    break
                if c == "X":
                    method = (cluster[n + 1:] or (args[j] if j < len(args) else "")).upper()
                    break
                if c == "G":
                    get = True
                if c in CURL_VALUE_SHORT:
                    break
    return method in WRITE_METHODS if method else body and not get


def wget_writes(args):
    """wget пишет: `--method=POST|PUT|PATCH|DELETE` либо `--post-data|--post-file|--body-data|--body-file`."""
    method, body = None, False
    for j, a in enumerate(args):
        opt, eq, val = a.partition("=")
        low = opt.lower()
        if low == "--method":
            method = (val if eq else args[j + 1] if j + 1 < len(args) else "").upper()
        elif low in WGET_BODY_LONG:
            body = True
    return method in WRITE_METHODS if method else body


def ps_http_writes(args):
    """Invoke-WebRequest/Invoke-RestMethod пишут: `-Method Post|Put|Patch|Delete` (`-Method:Post`, сокращения `-Meth`),
    без `-Method` — `-Body`, `-InFile`, `-Form`."""
    method, body = None, False
    for j, a in enumerate(args):
        if not a.startswith("-"):
            continue
        low, _, val = a.lower().partition(":")
        if re.fullmatch(r"-me(?:t(?:h(?:o(?:d)?)?)?)?", low):
            method = (val or (args[j + 1] if j + 1 < len(args) else "")).upper()
        elif re.fullmatch(r"-(?:bo(?:d(?:y)?)?|inf(?:i(?:l(?:e)?)?)?|fo(?:r(?:m)?)?)", low):
            body = True
    return method in WRITE_METHODS if method else body


def http_write_label(name, args):
    """curl/wget/Invoke-WebRequest/Invoke-RestMethod с записью на api.github.com или uploads.github.com → метка либо None.
    В PowerShell `curl` и `wget` — псевдонимы Invoke-WebRequest: их аргументы читаются обоими способами."""
    if name not in HTTP_NAMES:
        return None
    host = next((m.group(1).lower() for a in args for m in [GITHUB_API_HOST.search(a)] if m), None)
    if not host:
        return None
    parsers = {"curl": (curl_writes, ps_http_writes), "wget": (wget_writes, ps_http_writes)}.get(name, (ps_http_writes,))
    return f"{name}: запись на {host}" if any(p(args) for p in parsers) else None


def publish_label(name, args):
    """Метка публикации у команды `имя арг…` (таблица PUBLISH_SUBS; `python -m twine upload` — как `twine upload`;
    `gh api` и curl/wget/Invoke-WebRequest с записью — gh_api_label, http_write_label) либо None."""
    if PY_NAMES.match(name) and "-m" in args[:-1]:
        k = args.index("-m")
        name, args = cmd_name(args[k + 1]), args[k + 2:]
    if name == "gh":
        label = gh_api_label(args)
        if label:
            return label
    elif name in HTTP_NAMES:
        return http_write_label(name, args)
    pos = [a for a in args if not a.startswith("-")][:4]
    for sub in PUBLISH_SUBS.get(name, ()):
        if any(tuple(pos[i:i + len(sub)]) == sub for i in range(len(pos))):
            return f"{name} {' '.join(sub)}"
    return None
