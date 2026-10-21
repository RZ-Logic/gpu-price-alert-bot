"""Decide whether a retailer listing title is a specific GPU model.

INTERFACE CONTRACT (other modules depend on these names and signatures):

    class Kind(str, Enum): GPU, LAPTOP, PREBUILT, ACCESSORY, BUNDLE, OTHER

    @dataclass(frozen=True)
    class ParsedTitle:
        kind: Kind
        models: tuple[str, ...]   # canonical keys found, most prominent first,
                                  # same format as GPUModel.key ("RTX 5070 TI", "RX 9070 GRE", "ARC B580")
        vram_gb: int | None       # VRAM stated for the card, if any

    @dataclass(frozen=True)
    class MatchResult:
        ok: bool
        reason: str               # short, e.g. "match", "wrong model: RTX 5070 TI", "laptop", "vram 8 != 16"

    def parse_title(title: str) -> ParsedTitle
    def match(title: str, model: GPUModel) -> MatchResult

How it works:

1. normalise(): uppercase, accents dropped, trademark and other non-ASCII symbols
   become spaces, a possessive "'s" is removed ("RTX 4080's" is not a 4080 S),
   letter/digit runs are split ("RTX5070TI" -> "RTX 5070 TI"), and the title is
   compared as whole tokens only, so "TI" never matches inside "MULTI".
2. Model mentions are a family token (RTX, GTX, GT, GEFORCE, RX, RADEON, ARC)
   followed by a model number and an optional suffix. The suffix must match
   exactly: "RTX 5070" is only produced when no TI / SUPER token follows. NVIDIA "S"
   is SUPER ("4080S", "4070 Ti-S"); any other stray letter ("RTX5070T", "RX 9070 X T")
   is an unknown suffix that matches no catalog model. A revision ("RTX 4060 V2"), a
   glued OC or FE ("RTX5080FE") and a glued VRAM size ("RTX 508016GB") are not suffixes.
   Workstation (RTX PRO, Arc Pro, Radeon Pro), China (5090 D, D V2) and mobile
   parts get their own keys, so they never equal a desktop GPUModel.key.
   Vendor part codes that carry the model (Gigabyte GV-N507T..., GV-R9070XT...,
   MSI G506T16..., PNY VCG5070T..., Sapphire 11348-..., Sparkle SB580...) are
   decoded and added after the text mentions. A bare 4-digit number listed right
   after a mention ("RTX 5070 Ti/5070", "RX 9070 XT and 9070") is that family's
   model when it follows that family's numbering ("RX 9070 XT/1440p" is not). A more
   specific key ("RX 9070 GRE") replaces the base key when it comes from a part code,
   or when it comes first and the base is only a restatement in parentheses or before
   SERIES that is not listed after "/", AND or OR, or only a part code. Otherwise both
   stay ("RTX 5070 Ti / RTX 5070", "(RTX 5070 Ti / RTX 5070)", "RTX 5070 ... (RTX 5070
   Ti cooler design)"): multiple models.
3. VRAM is the "<n>GB" / "<n>G" size in the title, skipping system RAM and storage
   sizes. Two different sizes ("8GB/16GB", "8/16GB", "16GB vs 8GB") leave it unknown.
   "16GB DDR7" is VRAM on a card, and so is "16GB memory" right after the model or
   before GDDR ("RTX 5060 Ti 16GB Memory"); DDR4/DDR5, other memory ("RTX 5060 Ti
   GPU, 16GB Memory"), and memory in a laptop or prebuilt title are system RAM.
4. The listing kind uses word-boundary rules, with French wording alongside the
   English: non-card listings first (for parts, damaged or as-is, empty box, repair,
   rental, gift card, multi-card lots), then accessories (sag brackets, water blocks,
   risers, bridges, eGPU boxes, "no GPU" display models, "for RTX 5090" items), then
   laptops and prebuilt PCs (CPU plus RAM/SSD/OS specs; the word Laptop or a mobile
   marker outweighs GPU or GDDR in the title), then bundles (card plus PSU,
   motherboard, CPU, monitor or case; a game promotion such as "Battlefield 6
   Bundle" is not one).
   An accessory word right after "with" / "includes" or before "included" on a card
   ("with GPU holder", "bracket included") does not turn the card into an accessory,
   but a card noun right before one ("GPU Water Block") does not make it a card. Fan
   counts ("Triple Fan") are cooler descriptions, not fans sold alone.

match() is ok only for kind GPU, exactly one model equal to model.key, and,
when model.vram_gb is set, a parsed VRAM equal to it ("vram unknown" fails).
Otherwise a parsed VRAM must be one of model.vram_options ("RX 9070 12GB" is a
GRE with the suffix left out); a title with no VRAM still matches.
Price floors live in the pricing layer, not here.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

from gpu_alerts.catalog import GPUModel


class Kind(str, Enum):
    GPU = "gpu"
    LAPTOP = "laptop"
    PREBUILT = "prebuilt"
    ACCESSORY = "accessory"
    BUNDLE = "bundle"
    OTHER = "other"


@dataclass(frozen=True)
class ParsedTitle:
    kind: Kind
    models: tuple[str, ...]
    vram_gb: int | None


@dataclass(frozen=True)
class MatchResult:
    ok: bool
    reason: str


# Normalisation

_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_POSSESSIVE = re.compile(r"(?<=[0-9A-Z])['\u2019]S\b")
_GB_GDDR = re.compile(r"(?<=\d)GB(?=GDDR)")
_SPLIT_LETTER_DIGIT = re.compile(r"(?<=[A-Z])(?=\d)|(?<=\d)(?=[A-Z])")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_TOKEN = re.compile(r"[A-Z]+|\d+")
# A hyphenated part code with three or more parts: "ROG-ASTRAL-RTX5090DV2-O24G-BTF", "TUF-RTX4070TIS-O16G-GAMING".
_SKU_WORD = re.compile(r"(?<![A-Z0-9-])[A-Z0-9]+(?:-[A-Z0-9]+){2,}(?![A-Z0-9-])")


def _fold(title: str) -> str:
    """Accented letters lose their accents, so French "carte mere" and "defectueux" read as written.
    Only letters are folded: NFKD on the whole title would turn a trademark sign into "TM" glued to
    the family ("RTXTM 5080")."""
    return "".join(unicodedata.normalize("NFD", c)[0] if c.isalpha() and not c.isascii() else c for c in title)


def _clean(title: str) -> str:
    """Uppercase with accents, possessives, trademark symbols and other non-ASCII removed, punctuation kept."""
    t = _POSSESSIVE.sub("", _fold(title).upper())       # "RTX 4080's" is not the S shorthand for SUPER
    t = _NON_ASCII.sub(" ", t)
    t = _GB_GDDR.sub("GB ", t)      # "16GBGDDR7": the size and the memory type run together
    return re.sub(r"\s+", " ", t).strip()


def normalise(title: str) -> str:
    """Uppercase, split letter/digit runs and keep only alphanumeric tokens."""
    t = _SPLIT_LETTER_DIGIT.sub(" ", _clean(title))
    return _NON_ALNUM.sub(" ", t).strip()


# Model mentions

_NV_FAMILY = {"RTX", "GTX", "GT"}
_AMD_SUFFIX = {"XTX", "XT", "GRE"}
_ARC_BARE = {"B570", "B580"}   # A-series needs the ARC token ("A750GL" is a PSU)
_CHINA_D = {"4090", "5090"}
_MOBILE = {"LAPTOP", "MOBILE", "MAXQ"}
# Glued VRAM such as "RX 907016GB": 4-digit model then a VRAM size.
_GLUED_VRAM = re.compile(r"^(\d{4})(8|12|16|20|24|32)$")


@dataclass(frozen=True)
class _Mention:
    key: str
    pos: int            # token index of the model number
    mobile: bool = False
    code: bool = False  # read from a part code ("ROG-ASTRAL-RTX5090DV2-O24G-BTF", "GV-N507T...")
    aside: bool = False  # an unlisted restatement in parentheses or before SERIES ("(AMD Radeon RX 9070)")


def _nv_number_family(num: str) -> str:
    """Family for a bare GeForce number: RTX for 20 to 50 series, else GT."""
    if len(num) == 4 and num[0] in "2345" and num[1] == "0":
        return "RTX"
    return "GTX" if len(num) == 4 else "GT"


def _glued_suffix(tok: str, suffixes) -> bool:
    """A suffix run into other letters ("XTOC", "TIOC"). It becomes an unknown suffix so the
    title matches no catalog model, instead of being read as the base model."""
    return any(tok.startswith(s) and len(tok) > len(s) for s in suffixes)


def _unknown_letters(toks: list[str], k: int, glued: frozenset[int]) -> bool:
    """toks[k] is an unexpected letter suffix: a lone letter ("RTX 5070 T", "RX 9070 X T") or
    one or two letters run into the number ("RTX5090DD"). It becomes an unknown suffix so the
    title matches no catalog model, instead of being read as the base model. A letter that
    starts a VRAM code ("-O12G", "-P32G", "Z16G"), a revision ("RTX 4060 V2"), a glued OC or FE
    ("RTX5080FE"), mobile markers and an AMD mobile "M" are not suffixes."""
    tok = toks[k] if k < len(toks) else ""
    nxt = toks[k + 1] if k + 1 < len(toks) else ""
    nxt2 = toks[k + 2] if k + 2 < len(toks) else ""
    if not tok.isalpha() or tok in _MOBILE or tok in ("MAX", "M", "OC", "FE"):
        return False
    if len(tok) == 1 and k not in glued and k + 1 in glued and nxt.isdigit() and nxt2 in _VRAM_UNITS:
        return False
    if tok == "V" and k + 1 in glued and nxt.isdigit():
        return False
    return len(tok) == 1 or (k in glued and len(tok) == 2)


def _suffix_nv(toks: list[str], i: int, num: str, glued: frozenset[int] = frozenset()) -> tuple[str, int, bool]:
    """Suffix after an NVIDIA number at toks[i]; returns (suffix, tokens used, mobile)."""
    nxt = toks[i + 1] if i + 1 < len(toks) else ""
    nxt2 = toks[i + 2] if i + 2 < len(toks) else ""
    sized = _GLUED_VRAM.match(toks[i]) is not None and nxt in _VRAM_UNITS   # "RTX 508016GB": the unit, no suffix
    if sized:
        suffix, used = "", 0
    elif nxt == "TI" and nxt2 in ("SUPER", "S"):         # "4070 Ti S", "4070 Ti-S"
        suffix, used = "TI SUPER", 2
    elif nxt == "TIS":
        suffix, used = "TI SUPER", 1
    elif nxt == "TI":
        suffix, used = "TI", 1
    elif nxt == "T" and nxt2 == "I":                    # "5070 T i", "5070 T.I."
        suffix, used = "TI", 2
    elif nxt in ("SUPER", "S"):                         # "S" is shorthand: "4080S", "4080 S"
        suffix, used = "SUPER", 1
    elif nxt == "DV" and nxt2 == "2" and num in _CHINA_D:
        suffix, used = "D V2", 2          # ASUS code "RTX5090DV2"
    elif nxt == "D" and num in _CHINA_D and nxt2 != "RGB":     # not "RTX 5090 D-RGB Water Block"
        if nxt2 == "V" and i + 3 < len(toks) and toks[i + 3] == "2":
            suffix, used = "D V2", 3
        else:
            suffix, used = "D", 1
    elif nxt == "D" and nxt2 == "RGB":                 # "D-RGB" lighting, not a suffix
        suffix, used = "", 0
    elif nxt.startswith("TISUPER"):
        suffix, used = ("TI SUPER" if nxt == "TISUPER" else nxt), 1
    elif _glued_suffix(nxt, ("TI", "SUPER")) or _unknown_letters(toks, i + 1, glued):
        suffix, used = nxt, 1
    else:
        suffix, used = "", 0
    k = _past_vram(toks, i if sized else i + 1 + used)
    after = toks[k] if k < len(toks) else ""
    after2 = toks[k + 1] if k + 1 < len(toks) else ""
    mobile = after in _MOBILE or (after == "MAX" and after2 == "Q")
    return suffix, used, mobile


def _past_vram(toks: list[str], k: int) -> int:
    """Index after a VRAM size at toks[k] ("12GB", "8 GB GDDR7"), else k, so a mobile marker
    written after the size still counts ("RTX 5070 Ti 12GB Laptop GPU")."""
    if k + 1 < len(toks) and toks[k].isdigit() and toks[k + 1] in _VRAM_UNITS:
        k += 2
        if k < len(toks) and toks[k] == "GDDR":
            k += 1
            while k < len(toks) and (toks[k].isdigit() or toks[k] == "X"):
                k += 1
    return k


_LIST_WORDS = {"AND", "OR", "VS"}
_NOT_MODEL_UNITS = {"MHZ", "MB", "BIT", "GBPS"}
# Desktop model numbering: RTX 2050 to 5090; RX 5300 to 7900 (6650, 7650) and RX 9060 / 9070.
_MODEL_NUMBER = {"RTX": re.compile(r"[2-5]0[5-9]0"), "RX": re.compile(r"[567]\d[05]0|90[5-9]0")}


def _listed_number(toks: list[str], k: int, seps: frozenset[int], family: str) -> tuple[int, str]:
    """A bare model number listed right after a mention that ends before toks[k] ("RTX 5070 Ti/5070",
    "RX 9070 XT and 9070"): returns its index and the family it inherits, else (k, ""). Only a
    4-digit number in that family's numbering counts, not a clock, size, bus width, resolution or
    year ("RTX 5070 / 2512 MHz", "RX 9070 XT/1440p", "RTX 5090 / 2025 Edition"). A comparison
    ("Comparable to 4060Ti") is not a list."""
    if k < len(toks) and toks[k] in _LIST_WORDS:
        j = k + 1
    elif k in seps:
        j = k
    else:
        return k, ""
    num = toks[j] if j < len(toks) else ""
    after = toks[j + 1] if j + 1 < len(toks) else ""
    if not (num.isdigit() and len(num) == 4) or after in _NOT_MODEL_UNITS:
        return k, ""
    if family in _NV_FAMILY and _nv_number_family(num) != family:
        return k, ""
    if family in _MODEL_NUMBER and not _MODEL_NUMBER[family].fullmatch(num):
        return k, ""
    return j, family


def _text_mentions(toks: list[str], glued: frozenset[int] = frozenset(),
                   seps: frozenset[int] = frozenset()) -> list[_Mention]:
    """Model mentions in the token list; `glued` holds the indices of tokens written with no
    separator after the previous token ("5070" "T" in "RTX5070T"), `seps` those written right
    after a "/", "|" or "&"."""
    found: list[_Mention] = []
    n = len(toks)
    i = 0
    listed = ""     # family that a bare number listed at toks[i] inherits ("RTX 5070 Ti/5070")
    while i < n:
        t = toks[i]
        nxt = toks[i + 1] if i + 1 < n else ""
        inherit, listed = listed, ""

        # NVIDIA: [GEFORCE] [RTX|GTX|GT] [PRO] <number>
        if t in _NV_FAMILY or t == "GEFORCE" or inherit in _NV_FAMILY:
            j = i if inherit else i + 1
            family = inherit or (t if t in _NV_FAMILY else "")
            if family == "" and j < n and toks[j] in _NV_FAMILY:
                family = toks[j]
                j += 1
            pro = j < n and toks[j] == "PRO"
            if pro:
                j += 1
            if j + 1 < n and family == "RTX" and toks[j] == "A" and toks[j + 1].isdigit():
                found.append(_Mention(f"RTX A{toks[j + 1]}", j + 1))
                i = j + 2
                continue
            if j < n and toks[j].isdigit() and 3 <= len(toks[j]) <= 6:
                num = toks[j]
                glued_vram = _GLUED_VRAM.match(num)
                if glued_vram:
                    num = glued_vram.group(1)
                if len(num) <= 4:
                    family = family or _nv_number_family(num)
                    if pro:
                        found.append(_Mention(f"{family} PRO {num}", j))
                        i = j + 1
                        continue
                    suffix, used, mobile = _suffix_nv(toks, j, num, glued)
                    key = " ".join(p for p in (family, num, suffix) if p)
                    found.append(_Mention(key, j, mobile))
                    i, listed = _listed_number(toks, j + 1 + used, seps, family)
                    continue
            i += 1
            continue

        # AMD: [RADEON] [RX] [PRO] <number> [XTX|XT|GRE]
        if t in ("RX", "RADEON") or inherit == "RX":
            j = i if inherit else i + 1
            if t == "RADEON" and j < n and toks[j] == "RX":
                j += 1
            if j < n and toks[j] == "PRO":
                k = j + 1
                if k + 1 < n and toks[k].isalpha() and len(toks[k]) == 1 and toks[k + 1].isdigit():
                    found.append(_Mention(f"RADEON PRO {toks[k]}{toks[k + 1]}", k + 1))
                    i = k + 2
                    continue
                i = j + 1
                continue
            if j < n and toks[j].isdigit() and 3 <= len(toks[j]) <= 6:
                num = toks[j]
                glued_vram = _GLUED_VRAM.match(num)
                if glued_vram:
                    num = glued_vram.group(1)
                if len(num) <= 4:
                    nxt = toks[j + 1] if j + 1 < n else ""
                    used = 0
                    if not glued_vram and (nxt in _AMD_SUFFIX or _glued_suffix(nxt, _AMD_SUFFIX)
                                           or _unknown_letters(toks, j + 1, glued)):
                        used = 1
                        # A stray letter after the suffix ("RX 7900XT X") is not a known suffix either.
                        if nxt in _AMD_SUFFIX and _unknown_letters(toks, j + 2, glued) and len(toks[j + 2]) == 1:
                            used = 2
                    suffix = " ".join(toks[j + 1: j + 1 + used])
                    after = toks[j + 1 + used] if j + 1 + used < n else ""
                    k = _past_vram(toks, j if glued_vram else j + 1 + used)
                    key = " ".join(p for p in ("RX", num, suffix) if p)
                    found.append(_Mention(key, j, after == "M" or (toks[k] if k < n else "") in _MOBILE))
                    i, listed = _listed_number(toks, j + 1 + used, seps, "RX")
                    continue
            i += 1
            continue

        # Intel: [ARC] [PRO] <A|B> <number>, or a bare known Arc number.
        if t in ("ARC", "ARCPRO"):
            j = i + 1
            pro = t == "ARCPRO"
            if j < n and toks[j] == "PRO":
                pro = True
                j += 1
            if j + 1 < n and toks[j] in ("A", "B") and toks[j + 1].isdigit() and 2 <= len(toks[j + 1]) <= 3:
                name = f"{toks[j]}{toks[j + 1]}"
                found.append(_Mention(f"ARC PRO {name}" if pro else f"ARC {name}", j + 1))
                i = j + 2
                continue
            i += 1
            continue
        if t in ("A", "B") and nxt.isdigit() and f"{t}{nxt}" in _ARC_BARE:
            prev = toks[i - 1] if i else ""
            name = f"{t}{nxt}"
            found.append(_Mention(f"ARC PRO {name}" if prev == "PRO" else f"ARC {name}", i + 1))
            i += 2
            continue
        i += 1
    return found


# Vendor part codes, matched on the cleaned (unsplit) title.
_NV_CODE_SUFFIX = {"": "", "T": "TI", "S": "SUPER", "TS": "TI SUPER"}
_AMD_CODE_SUFFIX = {"": "", "X": "XT", "XT": "XT", "XTX": "XTX", "GRE": "GRE"}
_SAPPHIRE_CODE = re.compile(r"\b\d{5}-\d{2}-\d{2}G\b")
_SAPPHIRE = {"11348": "RX 9070 XT", "11349": "RX 9070", "11354": "RX 9070 GRE"}
_CODE_PATTERNS = (
    # Gigabyte NVIDIA: GV-N507TGAMING (5070 Ti), GV-N5070GAMING, GVN5070AERO.
    ("gb_nv3", re.compile(r"\bGV-?N(\d{3})(TS|T|S)(?=[A-Z])")),
    ("gb_nv4", re.compile(r"\bGV-?N(\d{4})(?=[A-Z])")),
    # Gigabyte AMD: GV-R9070XTGAMING, GV-R907XGAMING, GV-R907GREGAMING, GV-R76GAMING.
    ("gb_amd", re.compile(r"\bGV-?R(\d{2,4})(XTX|XT|X|GRE)?(?=[A-Z])")),
    # MSI: G506T16V2CP (5060 Ti 16GB), G5070-12S2C (5070 12GB).
    ("msi", re.compile(r"\bG([2-5]0[5-9])0?(TS|T|S)?-?(?:8|12|16|24|32)[A-Z]")),
    # PNY: VCG5070T16TFXXPB1 (5070 Ti 16GB).
    ("pny", re.compile(r"\bVCG([2-5]0[5-9]0)(TS|T|S)?(?:8|12|16|24|32)")),
    # XFX RX 9000: RX-97TMARGBA (9070 XT), RX-97SWFT3BA (9070), RX-96TS38GB7 (9060 XT).
    ("xfx", re.compile(r"\bRX-(9[67])(T?)(?=[A-Z])")),
    # Sapphire: 11348-01-20G (RX 9070 XT).
    ("sapphire", re.compile(r"\b(11348|11349|11354)-\d{2}-\d{2}G\b")),
    # Sparkle Arc: SB580T-12G, SB570G-10G.
    ("sparkle", re.compile(r"\bSB(5[78]0)[A-Z]")),
)


def _code_mentions(clean: str) -> list[str]:
    keys: list[str] = []
    for name, pattern in _CODE_PATTERNS:
        for m in pattern.finditer(clean):
            if name == "gb_nv3":
                keys.append(f"RTX {m.group(1)}0 {_NV_CODE_SUFFIX[m.group(2)]}".strip())
            elif name == "gb_nv4":
                keys.append(f"RTX {m.group(1)}")
            elif name == "gb_amd":
                num = m.group(1).ljust(4, "0")
                keys.append(f"RX {num} {_AMD_CODE_SUFFIX[m.group(2) or '']}".strip())
            elif name == "msi":
                keys.append(f"RTX {m.group(1)}0 {_NV_CODE_SUFFIX[m.group(2) or '']}".strip())
            elif name == "pny":
                keys.append(f"RTX {m.group(1)} {_NV_CODE_SUFFIX[m.group(2) or '']}".strip())
            elif name == "xfx":
                num = f"{m.group(1)[0]}0{m.group(1)[1]}0"   # "97" -> "9070"
                keys.append(f"RX {num} XT" if m.group(2) else f"RX {num}")
            elif name == "sapphire":
                keys.append(_SAPPHIRE[m.group(1)])
            elif name == "sparkle":
                keys.append(f"ARC B{m.group(1)}")
    return keys


def _key_parts(key: str) -> tuple[str, list[str]]:
    """("RTX 5070", ["TI"]) for "RTX 5070 TI"; base is family plus number."""
    parts = key.split()
    if len(parts) >= 3 and parts[1] == "PRO":
        return " ".join(parts[:3]), parts[3:]
    return " ".join(parts[:2]), parts[2:]


def _dedupe(mentions: list[_Mention]) -> tuple[str, ...]:
    """Unique keys in order, dropping a key when a more specific one of the same number shadows it.

    "RX 9070 GRE ... (AMD Radeon RX 9070)" is one GRE card, not two models. A more specific key
    shadows the base when it comes from a part code, or when it is mentioned first and every
    text mention of the base is a restatement (in parentheses or before SERIES) or the base comes
    only from a part code. A list ("RTX 5070 Ti / RTX 5070", "RX 9070 XT and RX 9070", also inside
    parentheses or before SERIES: "(RTX 5070 Ti / RTX 5070)") or a later specific mention ("RTX 5070
    12G Ventus (RTX 5070 Ti cooler design)", "RX 9070 - beats RX 9070 XT") keeps both keys, so
    match() rejects the title as multiple models.
    """
    first: dict[str, float] = {}
    code: dict[str, bool] = {}
    aside: dict[str, bool] = {}
    for mn in mentions:
        first.setdefault(mn.key, float("inf"))
        if mn.code:
            code[mn.key] = True
        else:
            first[mn.key] = min(first[mn.key], mn.pos)
            aside[mn.key] = aside.get(mn.key, True) and mn.aside
    out = []
    for k in first:
        base, suffix = _key_parts(k)
        shadowed = any(
            other != k
            and _key_parts(other)[0] == base
            and len(_key_parts(other)[1]) > len(suffix)
            and _key_parts(other)[1][: len(suffix)] == suffix
            and (code.get(other, False) or (first[other] <= first[k] and aside.get(k, True)))
            for other in first
        )
        if not shadowed:
            out.append(k)
    return tuple(out)


# VRAM

_VRAM_SIZES = {2, 3, 4, 6, 8, 10, 11, 12, 16, 20, 24, 32, 48, 96}
_VRAM_UNITS = {"GB", "G", "GO", "GD"}   # "GD": Gigabyte codes such as GV-N506TGAMING OC-16GD
_NOT_VRAM_AFTER = {"RAM", "DDR", "LPDDR", "SSD", "NVME", "EMMC", "STORAGE", "UNIFIED", "SYSTEM", "MEMORY"}
_STORAGE_WORDS = _NOT_VRAM_AFTER | {"HDD", "HARD", "DRIVE"}
# Two sizes sharing one unit: "8/16GB", "8 or 16GB", "8|16 GB". Not "x8/16GB" (PCIe lanes) or "8-16GB".
_SHARED_UNIT = re.compile(r"(?<![\w.-])(\d{1,2}) ?(?:/|\||\bOR\b) ?(\d{1,2}) ?G[BO]?\b")


def _after_model(toks: list[str], i: int) -> bool:
    """toks[i] comes right after the model number or suffix, or after WITH there ("RTX 5060 Ti 16GB
    memory", "RX 9060 XT with 16GB memory")."""
    k = i - 2 if i > 1 and toks[i - 1] == "WITH" else i - 1
    prev = toks[k] if k >= 0 else ""
    return prev in _AMD_SUFFIX or prev in ("TI", "SUPER") or (prev.isdigit() and len(prev) == 4)


def _vram(toks: list[str], clean: str = "", computer: bool = False) -> int | None:
    """VRAM size for the card, or None when the title gives none or two different sizes.

    "8GB/16GB", "8/16GB" or "16GB vs 8GB" names two variants, so neither VRAM-specific alert may fire.
    `computer` is set for laptop and prebuilt titles, where "16GB memory" is system RAM. On a card it is
    VRAM only right after the model or before GDDR: "RTX 5060 Ti GPU, 16GB Memory" is a prebuilt that
    the kind rules missed.
    """
    for m in _SHARED_UNIT.finditer(clean):
        a, b = int(m.group(1)), int(m.group(2))
        if a != b and a in _VRAM_SIZES and b in _VRAM_SIZES:
            return None
    candidates: list[tuple[int, int]] = []
    for i, t in enumerate(toks[:-1]):
        if not t.isdigit() or toks[i + 1] not in _VRAM_UNITS:
            continue
        size = int(t)
        if size not in _VRAM_SIZES:
            glued = _GLUED_VRAM.match(t)
            if not glued:
                continue
            size = int(glued.group(2))
        after = toks[i + 2] if i + 2 < len(toks) else ""
        after2 = toks[i + 3] if i + 3 < len(toks) else ""
        # "16GB DDR7" on a card is VRAM, and so is "16GB memory" after the model or before GDDR; DDR4/DDR5,
        # other memory, or memory in a PC title, is system RAM.
        vram_word = (after == "DDR" and after2 in ("6", "7")) or (
            after == "MEMORY" and not computer and (after2 == "GDDR" or _after_model(toks, i)))
        if after in _NOT_VRAM_AFTER and not vram_word:
            continue
        # "32GB 1TB", "16GB 512GB" or "16GB 32GB SSD": system RAM then storage. "8GB/16GB" is two
        # VRAM variants, so both sizes stay candidates.
        if after.isdigit() and (after2 == "TB" or (after2 == "GB" and (
                int(after) not in _VRAM_SIZES or (toks[i + 4] if i + 4 < len(toks) else "") in _STORAGE_WORDS))):
            continue
        candidates.append((i, size))
    sizes = {size for _, size in candidates}
    return sizes.pop() if len(sizes) == 1 else None


# Kind

def _rx(*phrases: str) -> re.Pattern:
    return re.compile(r"\b(?:" + "|".join(phrases) + r")\b")


_NO_GPU = _rx(r"NO GPU", r"WITHOUT (?:THE )?GPU", r"GPU NOT INCLUDED", r"DECORATION", r"DECORATIVE",
              r"COLLECTIBLE", r"DUMMY", r"PLACEHOLDER", r"MODEL KIT", r"FIGURE", r"TOY", r"LEGO")
# Not one working card for sale: damaged, an empty box, a service, a gift card or a multi-card lot.
_NOT_A_CARD = _rx(r"FOR PARTS", r"PARTS ONLY", r"NOT WORKING", r"NON[- ]?WORKING", r"DEFECTIVE", r"BROKEN",
                  r"FAULTY", r"DAMAGED", r"CRACKED", r"MELTED", r"BURN(?:T|ED)", r"ARTIFACT\w*", r"UNTESTED",
                  r"NOT TESTED", r"NO (?:DISPLAY|VIDEO|SIGNAL|POST)", r"AS[- ]?IS", r"EMPTY BOX", r"BOX ONLY",
                  r"BOX AND MANUAL", r"REPAIR", r"RENTAL", r"GIFT CARD", r"LOT OF \d+", r"\d+ ?-?PACK",
                  # French: empty box, for parts, defective, not working, as is.
                  r"BOITE VIDE", r"BOITE SEULEMENT", r"POUR PIECES", r"DEFECTUEU(?:X|SES?)", r"NE FONCTIONNE PAS",
                  r"TEL(?:LE)? QUEL(?:LE)?")
_ACCESSORY = _rx(
    r"WATER[ -]?BLOCKS?", r"(?:LIQUID |WATER )?COOLING BLOCK", r"GPU BLOCK", r"GPU BACKPLATE", r"POWER CORD",
    r"SUPPORT PILLAR", r"RISERS?", r"RISER CABLE",
    r"EXTENSION CABLE", r"POWER CABLE", r"SLEEVED CABLE", r"CABLE KIT",
    r"SAG", r"ANTI-SAG", r"BRACE", r"HOLDER", r"SUPPORT BRACKET", r"GPU BRACKET", r"GPU SUPPORT",
    r"GPU STAND", r"VERTICAL (?:GPU )?MOUNT", r"VERTICAL GPU", r"SLI", r"BRIDGE",
    r"E-?GPU", r"AI BOX", r"GAMING BOX", r"GPU BOX", r"EXTERNAL GRAPHICS", r"EXTERNAL GPU", r"ENCLOSURE",
    r"GRAPHICS DOCK", r"GPU DOCK",
    r"DOCKING STATION", r"XG MOBILE", r"OCULINK", r"GPU FANS?", r"REPLACEMENT",
    r"(?:BACKPLATE|HEATSINK|COOLER|CABLE|ADAPTER|MOUNT|COVER|SHROUD|THERMAL PADS?) FOR",
    r"SKIN", r"STICKERS?", r"DECALS?", r"MID TOWER", r"FULL TOWER", r"TOWER CASE", r"PC CASE",
    r"ATX CASE", r"COMPUTER CASE", r"GAMING CASE",
    r"COMPATIBLE (?:WITH|FOR) (?:\w+ ){0,2}(?:RTX|RX|GEFORCE|RADEON|NVIDIA|ASUS|MSI|ASROCK|GIGABYTE)",
    # Custom-loop parts that never say "water block": "EK-Quantum Vector3 FE RTX 5090 D-RGB ABP Set, Nickel + Plexi".
    r"EK-QUANTUM", r"ABP SET", r"PLEXI", r"ACETAL",
    # French water block and power cable.
    r"BLOC D.EAU", r"CABLE D.ALIMENTATION",
    # "WireView Pro for RTX 5090", "Ventilateur pour RTX 5080": the model follows "for", so the item is
    # made for a card.
    r"(?<!FIT )(?:FOR|POUR) (?:[\w-]+ ){0,3}(?:RTX|GTX|RX|GEFORCE|RADEON|ARC) ?[AB]?\d{3,4}",
)
# Accessory wording that real cards also use as feature text ("Triple fans for better cooling",
# "Compatible with Intel and AMD Platforms", "HDMI/DP Nvlink"): an accessory only when no card noun
# and model say the item is a card.
_ACCESSORY_FEATURE = _rx(r"NVLINK", r"FANS? FOR", r"COMPATIBLE (?:WITH|FOR) (?:\w+ ){0,2}(?:AMD|INTEL|MOST|ALL)")
# "with free game bundle" is still the card at the card's price.
_GAME_BUNDLE = _rx(r"(?:FREE )?GAMES? (?:CODE )?BUNDLE", r"GAMING BUNDLE", r"BUNDLE WITH (?:\w+ ){0,3}GAMES?")
# Words that only mean "accessory" when nothing says the item is a card itself.
_WEAK_ACCESSORY = _rx(r"CABLES?", r"ADAPTERS?", r"BACKPLATE", r"COOLER", r"HEATSINK", r"FANS?",
                      r"MOUNT", r"CASE", r"BRACKET", r"STAND", r"COVER", r"SHROUD", r"THUNDERBOLT",
                      r"THERMAL PADS?", r"THERMAL PASTE")
# Cooler descriptions on real cards ("Triple Fan", "Axial-tech Fans") are not accessories.
_FAN_FEATURE = _rx(r"(?:SINGLE|DUAL|DOUBLE|TWIN|TRIPLE|THREE|TWO|QUAD|FOUR|\d ?X|\d+ ?MM|AXIAL-TECH|HAWK|"
                   r"TORX|WINDFORCE|FROSTBLADE|0DB|SILENT|COOLING)[ -]?FANS?")
_CARD_WORD = _rx(r"GRAPHICS? CARDS?", r"VIDEO CARDS?", r"GPU", r"CARTES? GRAPHIQUES?", r"CARTES? VIDEO")
_CARD_NOUN = _rx(r"GRAPHICS? CARDS?", r"VIDEO CARDS?", r"GPU", r"CARTES? GRAPHIQUES?", r"CARTES? VIDEO",
                 r"GDDR\d?X?")
_VRAM_HINT = _rx(r"\d+ ?GB", r"\d+G")
# "GPU Water Block", "GPU Cooler": a card noun right before an accessory word names what the accessory
# fits, so it does not say the item is a card.
_CARD_NOUN_OF_ACCESSORY = re.compile(
    _CARD_NOUN.pattern + r"[ -](?=" + _ACCESSORY.pattern + "|" + _WEAK_ACCESSORY.pattern + ")")

_LAPTOP_WORD = _rx(r"LAPTOPS?", r"NOTEBOOKS?", r"CHROMEBOOK", r"ULTRABOOK", r"2-IN-1",
                   r"ORDINATEURS? PORTABLES?", r"PC PORTABLES?", r"PORTABLES? DE JEU")   # not bare PORTABLE
_LAPTOP_SERIES = _rx(
    r"ZENBOOK", r"VIVOBOOK", r"EXPERTBOOK", r"PROART P16", r"THINKPAD", r"THINKBOOK", r"IDEAPAD", r"YOGA",
    r"MATEBOOK", r"GALAXY BOOK\d?", r"SURFACE LAPTOP", r"MACBOOK", r"LG GRAM", r"GRAM PRO", r"ZEPHYRUS",
    r"STRIX SCAR",
    r"STRIX G1[4-8]", r"TUF A1[4-8]", r"TUF F1[4-8]", r"PREDATOR HELIOS", r"HELIOS NEO", r"HELIOS",
    r"NITRO V(?: ?1[4-8])?", r"ASPIRE", r"TRAVELMATE", r"LATITUDE", r"INSPIRON", r"XPS 1[3-7]", r"ELITEBOOK",
    r"PROBOOK", r"VICTUS", r"SPECTRE", r"OMEN MAX", r"OMEN TRANSCEND", r"BLADE 1[4-8]",
    r"RAZER BLADE", r"RAIDER A?1[4-8]", r"TITAN 18", r"VECTOR (?:A?1[4-8]|16|18)",
    r"CROSSHAIR", r"KATANA", r"CYBORG", r"SWORD 1[4-8]", r"PRESTIGE 1[3-6]", r"MODERN 1[4-5]",
    r"LEGION (?:PRO )?[579]I?\b(?! TOWER)", r"LEGION 9I", r"AORUS MASTER 1[4-8]", r"AORUS 1[4-8]",
    r"AERO X?1[4-8]", r"AREA-51 1[4-8]", r"ALIENWARE 1[4-8]", r"AURORA 1[4-8]X?", r"PULSE A?1[4-8]",
    r"STEALTH A?1[4-8]", r"COPILOT\+ PC NOTEBOOK", r"LOQ", r"FLOW Z13", r"OMEN 1[4-8]", r"NITRO 1[4-8]",
    r"TUF GAMING [AF]1[4-8]", r"AORUS ELITE 1[4-8]", r"DELL 1[4-6] PLUS",
)
_SCREEN_INCHES = re.compile(r"(?<![\d.])1[0-8](?:\.\d)?(?: ?\"| ?''| ?IN\b| ?INCH)")
_MOBILE_CPU = re.compile(r"\b\d{3,5}(?:HX|HS|H|U|V)\b|\bAI ?[579] (?:HX )?\d{3}\b|\bAI\+? ?\d? ?HX\b")
_COMPUTER_PHRASE = _rx(r"GAMING PC", r"GAMING DESKTOP", r"DESKTOP PC", r"DESKTOP COMPUTER", r"MINI PC",
                       r"NUC", r"BRIX", r"AIO", r"ALL-IN-ONE", r"TOWER PC", r"GAMING COMPUTER",
                       r"GAMING TOWER", r"WORKSTATION PC",
                       r"ORDINATEURS? DE BUREAU", r"ORDINATEURS? DE JEU", r"PC DE JEU", r"TOUR DE JEU")
_CPU = _rx(r"RYZEN", r"CORE I[3579]", r"CORE ULTRA", r"INTEL ULTRA", r"ULTRA [579X]\d?", r"U[579]",
           r"I[3579]-\d{4,5}[A-Z]{0,2}", r"INTEL I[3579] \d{4,5}[A-Z]{0,2}", r"CORE [3579][ -]\d{3}[A-Z]{0,2}",
           r"R[579] \d{4}[A-Z0-9]*", r"\d{4}X3D2?", r"XEON", r"THREADRIPPER",
           r"CELERON", r"PENTIUM", r"SNAPDRAGON", r"APPLE M\d")
_SPEC = _rx(r"\d+ ?GB (?:DDR\d )?RAM", r"\d+ ?TB", r"\d+ ?GB SSD", r"SSD", r"NVME", r"WIN ?1[01]\w*",
            r"W1[01][HP]?", r"WINDOWS", r"NO OS", r"\d+ ?GB \d+ ?(?:GB|TB)", r"DDR5 RAM", r"DDR\d MEMORY",
            r"\d+ ?GB DDR[45]")   # system RAM; "16GB DDR7" on a card is VRAM
# A card's own M.2 slot ("RTX 4060 Ti SSD ... M.2 NVMe SSD Slot") is not a PC's storage.
_SSD_SLOT = re.compile(r"\b(?:M\.2 )?(?:NVME )?SSD SLOTS?\b|\bNVME SLOTS?\b")
_CPU_NUMBER = re.compile(r"[ -]?(?:AI\+? ?)?\d")     # after a CPU brand: "Ryzen 7", "Ryzen AI 9"
_BUNDLE = _rx(r"BUNDLE", r"COMBO")
# A hardware bundle names its second product; "Battlefield 6 Bundle" alone is a game promotion.
_BUNDLE_HARDWARE = _rx(r"PSU", r"POWER SUPPLY", r"MOTHERBOARD", r"PROCESSOR", r"CPU", r"MONITOR", r"CASE",
                       r"RAM", r"SSD")
# A second product after +, &, AND or WITH (French AVEC, ET): "GPU + TUF Gaming 850W Gold PSU",
# "+ PRO-B650-S Motherboard", "avec bloc d'alimentation". After "+", a case or a wattage names one too:
# "+ Prime AP202 mATX Case", "+ MSI MAG A850GL 850W". A comma ends the phrase, so "GDDR7, 750W PSU
# recommended" is not a bundle.
_BUNDLE_ITEM = re.compile(
    r"(?:\+|&|\bAND\b|\bWITH\b|\bW/|\bAVEC\b|\bET\b)\s*(?:[\w+-]+\s+){0,6}?"
    r"(?:PSU|POWER SUPPLY|MOTHERBOARD|PROCESSOR|CPU|MONITOR|PC CASE"
    r"|BLOC D.ALIMENTATION|CARTE MERE|PROCESSEUR|ECRAN)\b"
    r"|\+\s*(?:[\w+-]+\s+){0,6}?(?:CASE|\d{3,4} ?W)\b"
)
_OTHER_PRODUCT = _rx(r"PROCESSOR", r"MOTHERBOARD", r"DESKTOP MEMORY", r"RAM DDR\d", r"MONITOR", r"SMART TV",
                     r"TV", r"PROJECTOR", r"POWER SUPPLY", r"PSU", r"AUDIO INTERFACE", r"SOFTWARE",
                     r"LICENSE", r"HEADPHONES?", r"HEADSET", r"KEYBOARD", r"MOUSE", r"LCD SCREEN",
                     r"SCREEN")


_FOR_BEFORE = re.compile(r"\b(?:FOR|WITH|COMPATIBLE|POUR|AVEC)\b(?:\s+\S+){0,2}\s*$")
_INCLUDED_BEFORE = re.compile(r"(?:\bWITH|\bINCLUDES?|\bINCLUDED|\bINCLUDING|\bBUNDLED|\bW/|\+)(?:\s+\S+){0,2}\s*$")
_INCLUDED_AFTER = re.compile(r"(?:\s+\S+){0,2}?\s+INCLUDED\b")


def _has(pattern: re.Pattern, text: str, skip_after: re.Pattern | None = None,
         skip_before: re.Pattern | None = None) -> bool:
    """Any match of pattern in text, ignoring matches right after skip_after (e.g. "card for gaming PC")
    or right before skip_before (e.g. "bracket included")."""
    for m in pattern.finditer(text):
        if skip_after is not None and skip_after.search(text[max(0, m.start() - 24): m.start()]):
            continue
        if skip_before is not None and skip_before.match(text, m.end()):
            continue
        return True
    return False


def _cpu_with_number(clean: str) -> bool:
    """A CPU named with its model number ("Ryzen 7 9800X3D", "i7-14700F", "Ultra 9 275HX"), not a bare
    brand in card feature text ("AMD Smart Access Memory with Ryzen")."""
    return any(any(c.isdigit() for c in m.group()) or _CPU_NUMBER.match(clean, m.end())
               for m in _CPU.finditer(clean))


def _kind(clean: str, models: tuple[str, ...], mobile: bool) -> Kind:
    card = _has(_CARD_NOUN, clean)
    card_word = _has(_CARD_WORD, clean)    # GPU or graphics card, not just GDDR
    vram_hint = _has(_VRAM_HINT, clean)
    card_itself = _has(_CARD_NOUN, _CARD_NOUN_OF_ACCESSORY.sub(" ", clean))
    # A card sold "with GPU holder" or "+ rear bracket" is still a card.
    included_ok = (card_itself or vram_hint) and models
    if _has(_NOT_A_CARD, clean):
        return Kind.OTHER
    if (_has(_NO_GPU, clean)
            or _has(_ACCESSORY, clean, *((_INCLUDED_BEFORE, _INCLUDED_AFTER) if included_ok else ()))
            or (_has(_ACCESSORY_FEATURE, clean) and not (card_itself and models))):
        return Kind.ACCESSORY

    laptop_word = _has(_LAPTOP_WORD, clean, skip_after=_FOR_BEFORE)
    laptop_named = laptop_word or _has(_LAPTOP_SERIES, clean)
    laptop_cue = laptop_named or _SCREEN_INCHES.search(clean) is not None
    computer_phrase = _has(_COMPUTER_PHRASE, clean, skip_after=_FOR_BEFORE)
    cpu = _has(_CPU, clean)
    spec_hits = {m.group() for m in _SPEC.finditer(_SSD_SLOT.sub(" ", clean))}

    if mobile and not computer_phrase:
        return Kind.LAPTOP
    if not models and _has(_OTHER_PRODUCT, clean) and not laptop_cue and not (cpu and spec_hits):
        return Kind.OTHER
    computer = (
        (cpu and (spec_hits or laptop_cue or not card or (models and _cpu_with_number(clean))))
        or len(spec_hits) >= 2
        or (computer_phrase and (cpu or spec_hits or not card))
        or laptop_word      # "Laptop GPU", "Gaming Laptop ... GDDR7"; "card for laptop" was skipped above
    )
    if computer:
        if computer_phrase and not laptop_word:
            return Kind.PREBUILT
        if laptop_cue or _MOBILE_CPU.search(clean):
            return Kind.LAPTOP
        return Kind.PREBUILT
    if _has(_WEAK_ACCESSORY, _FAN_FEATURE.sub(" ", clean)) and not (card_itself or vram_hint):
        return Kind.ACCESSORY
    bundle_words = {m.group() for m in _BUNDLE.finditer(_GAME_BUNDLE.sub(" ", clean))}
    if models and ("COMBO" in bundle_words or (bundle_words and _has(_BUNDLE_HARDWARE, clean))
                   or _BUNDLE_ITEM.search(clean)):
        return Kind.BUNDLE
    # A laptop name ("MSI Sword 16 HX RTX 5070 8GB GDDR7") outweighs a VRAM size or GDDR; a screen size
    # alone does not, and GPU or graphics card in the title keeps it a card.
    if laptop_cue and not card_word and (laptop_named or not (card or vram_hint)):
        return Kind.LAPTOP
    if not models and not card:
        return Kind.OTHER
    return Kind.GPU


# Public API

def _is_aside(text: str, start: int, after: list[str]) -> bool:
    """A mention at text[start] inside parentheses or followed by SERIES restates the model."""
    return text.count("(", 0, start) > text.count(")", 0, start) or after == ["SERIES"]


_BRAND_WORDS = {"NVIDIA", "GEFORCE", "AMD", "RADEON", "RX"} | _NV_FAMILY


def _is_listed(toks: list[str], seps: frozenset[int], pos: int) -> bool:
    """The mention whose number is toks[pos] comes right after "/", "|", "&", AND, OR or VS, brand
    words included ("RTX 5070 Ti / NVIDIA GeForce RTX 5070 Series"): an alternative, never a restatement."""
    k = pos
    while k > 0 and toks[k - 1] in _BRAND_WORDS:
        k -= 1
    return k in seps or (k > 0 and toks[k - 1] in _LIST_WORDS)


def parse_title(title: str) -> ParsedTitle:
    clean = _clean(title)
    # Sapphire part numbers end in "-20G", which is not VRAM; drop them before tokenising.
    text = _SAPPHIRE_CODE.sub(" ", clean)
    # Same tokens as normalise(text).split(), plus where each one sits in the text.
    spans = [(m.group(), m.start(), m.end()) for m in _TOKEN.finditer(text)]
    toks = [t for t, _, _ in spans]
    glued = frozenset(k for k in range(1, len(spans)) if spans[k][1] == spans[k - 1][2])
    seps = frozenset(k for k in range(1, len(spans)) if set(text[spans[k - 1][2]:spans[k][1]]) & set("/|&"))
    sku_spans = [(m.start(), m.end()) for m in _SKU_WORD.finditer(text)]
    mentions = [
        _Mention(mn.key, mn.pos, mn.mobile, any(a <= spans[mn.pos][1] < b for a, b in sku_spans),
                 _is_aside(text, spans[mn.pos][1], toks[mn.pos + 1: mn.pos + 2])
                 and not _is_listed(toks, seps, mn.pos))
        for mn in _text_mentions(toks, glued, seps)
    ]
    mobile = any(mn.mobile for mn in mentions)
    models = _dedupe(mentions + [_Mention(k, len(toks), code=True) for k in _code_mentions(clean)])
    kind = _kind(clean, models, mobile)
    return ParsedTitle(kind=kind, models=models, vram_gb=_vram(toks, text, kind in (Kind.LAPTOP, Kind.PREBUILT)))


def match(title: str, model: GPUModel) -> MatchResult:
    """Is this listing title exactly `model` (desktop card, right suffix, right VRAM)?"""
    p = parse_title(title)
    if p.kind is not Kind.GPU:
        return MatchResult(False, p.kind.value)
    if not p.models:
        return MatchResult(False, "no model")
    if p.models[0] != model.key:
        return MatchResult(False, f"wrong model: {p.models[0]}")
    if len(p.models) > 1:
        return MatchResult(False, "multiple models: " + ", ".join(p.models))
    if model.vram_gb is not None:
        if p.vram_gb is None:
            return MatchResult(False, "vram unknown")
        if p.vram_gb != model.vram_gb:
            return MatchResult(False, f"vram {p.vram_gb} != {model.vram_gb}")
    elif model.vram_options and p.vram_gb is not None and p.vram_gb not in model.vram_options:
        # "RX 9070 12GB" is a GRE and "RTX 5090 24GB" a China D V2 with the suffix left out.
        return MatchResult(False, f"vram {p.vram_gb} != {'/'.join(map(str, model.vram_options))}")
    return MatchResult(True, "match")
