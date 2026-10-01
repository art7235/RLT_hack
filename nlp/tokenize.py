import re
_TOKEN = re.compile(r"[а-яa-z0-9]+(?:[-./][а-яa-z0-9]+)*")
def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")
def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))
