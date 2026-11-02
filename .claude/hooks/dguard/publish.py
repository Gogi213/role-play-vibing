"""Публикации кроме `git push` (его разбирает git_scan): выпуск и слияние PR через `gh` — запуску роли без согласия закрыты."""


PUBLISH_SUBS = {"gh": (("release", "create"), ("release", "upload"), ("release", "edit"), ("pr", "merge"))}


def publish_label(name, args):
    """Метка публикации у команды `имя арг…` либо None."""
    pos = [a for a in args if not a.startswith("-")][:4]
    for sub in PUBLISH_SUBS.get(name, ()):
        if any(tuple(pos[i:i + len(sub)]) == sub for i in range(len(pos))):
            return f"{name} {' '.join(sub)}"
    return None
