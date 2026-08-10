import email
import difflib
import json
import os
import re
import socket
import sys
import time
import traceback
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from imapclient import IMAPClient


# QQ IMAP configuration
IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
# QQ_AUTH_CODE is the IMAP authorization code generated in QQ Mail settings,
# not the password used to sign in to QQ.
QQ_USER = "2051588081@qq.com"
QQ_AUTH_CODE = "qyxmdoobbbukfagb"

# Optional Google Apps Script webhook. When empty, parsed orders are only printed.
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "").strip()
WEBHOOK_TOKEN = os.getenv("WEBHOOK_TOKEN", "").strip()

STATE_FILE = Path(__file__).with_name("qq_imap_state.json")
PERSONALIZATION_RULES_FILE = Path(__file__).with_name("personalization_rules.json")
ORDER_PATTERN = re.compile(
    r"You made a sale|sale on Etsy|Congratulations on your Etsy order",
    re.IGNORECASE,
)

# Etsy listing personalization prompt. Replace this text when a product uses a
# different set of fields. Numbered titles are used as the output field names.
PERSONALIZATION_TEMPLATE = """\
1. Cover Colour (e.g. Black)
2. Font Style (e.g. Font #1)
3. Foil Colour (e.g. Gold)
4. Names on Cover (e.g. Andy & Jessica)
5. Date (Format: MM.DD.YYYY, e.g. 12.20.2024)
6. Spine Text (Max 5-7 words)
7. Phone Number for Delivery (Required by courier)
"""

# Common customer wording that differs from the listing's field titles.
PERSONALIZATION_ALIASES = {
    "Cover Colour": (
        "cover colour",
        "cover color",
        "cover shade",
        "book colour",
        "book color",
    ),
    "Font Style": (
        "font style",
        "font number",
        "font no",
        "font #",
        "font",
        "font choice",
        "typeface",
    ),
    "Foil Colour": (
        "foil colour",
        "foil color",
        "font colour",
        "font color",
        "text colour",
        "text color",
        "lettering colour",
        "lettering color",
        "writing colour",
        "writing color",
    ),
    "Names on Cover": (
        "names on cover",
        "name on cover",
        "cover names",
        "cover name",
        "front cover text",
        "cover text",
    ),
    "Date": (
        "date",
        "cover date",
        "wedding date",
        "event date",
    ),
    "Spine Text": (
        "spine text",
        "text on spine",
        "spine wording",
    ),
    "Phone Number for Delivery": (
        "phone number for delivery",
        "delivery phone number",
        "delivery phone",
        "phone number",
        "phone #",
        "phone",
        "contact phone",
        "contact number",
        "courier phone",
        "mobile",
        "telephone",
        "telephone number",
    ),
}

# Value-based hints for customers who omit every field label. These lists are
# intentionally conservative: ambiguous values should remain in "其他".
COVER_COLOUR_VALUES = {
    "black",
    "white",
    "ivory",
    "cream",
    "beige",
    "brown",
    "red",
    "burgundy",
    "maroon",
    "pink",
    "blush",
    "orange",
    "yellow",
    "green",
    "sage",
    "olive",
    "blue",
    "navy",
    "navy blue",
    "light blue",
    "purple",
    "lavender",
    "grey",
    "gray",
    "charcoal",
    "teal",
}

FOIL_COLOUR_VALUES = {
    "gold",
    "silver",
    "rose gold",
    "copper",
    "bronze",
    "holographic",
    "rainbow",
}

SPINE_TEXT_HINT_PATTERN = re.compile(
    r"\b(?:our|wedding|guest\s+book|story|chapter|forever|love)\b",
    re.IGNORECASE,
)

SHOP_CONFIG = [
    {"sheetName": "小彩灯", "shopName": "Memiya", "currency": "USD"},
    {"sheetName": "3号店", "shopName": "LuxeJoy", "currency": "CAD"},
    {"sheetName": "14号店", "shopName": "NuviaAlbum", "currency": "CAD"},
    {"sheetName": "15号店", "shopName": "Mivow", "currency": "USD"},
    {"sheetName": "16号店", "shopName": "Mmovia", "currency": "USD"},
    {"sheetName": "17号店", "shopName": "ObiaMoment", "currency": "USD"},
    {"sheetName": "18号店", "shopName": "Sogave", "currency": "USD"},
]

SHOP_MAP = {
    item["shopName"].casefold(): item
    for item in SHOP_CONFIG
}


class ConfigurationError(ValueError):
    """A non-transient configuration problem that reconnecting cannot fix."""


def configure_utf8_console():
    """Prevent Chinese output from failing on ASCII-configured consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


class HTMLTextExtractor(HTMLParser):
    """Convert HTML into plain visible text and preserve useful line breaks."""

    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "thead",
        "tfoot",
        "tr",
        "ul",
    }
    SKIP_TAGS = {"script", "style", "head", "noscript"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip_depth = 0

    def add_newline(self):
        if not self.parts or self.parts[-1] != "\n":
            self.parts.append("\n")

    def add_space(self):
        if self.parts and not self.parts[-1].endswith((" ", "\n", "\t")):
            self.parts.append(" ")

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"br", "hr"} or tag in self.BLOCK_TAGS:
            self.add_newline()
        elif tag in {"td", "th"}:
            self.add_space()

    def handle_startendtag(self, tag, attrs):
        if tag.lower() in {"br", "hr"}:
            self.add_newline()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self.SKIP_TAGS:
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag == "tr" or tag in self.BLOCK_TAGS:
            self.add_newline()
        elif tag in {"td", "th"}:
            self.add_space()

    def handle_data(self, data):
        if not self.skip_depth:
            self.parts.append(data)

    def get_text(self):
        text = "".join(self.parts)
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\xa0", " ")

        lines = []
        for raw_line in text.split("\n"):
            line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip()
            if line:
                lines.append(line)
        return "\n".join(lines).strip()


def html_to_text(html_content):
    parser = HTMLTextExtractor()
    parser.feed(html_content)
    parser.close()
    return parser.get_text()


def decode_header_value(value):
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def decode_payload(payload, charset):
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def normalize_plain_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\xa0", " ")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


def extract_message_body(message):
    """Return the email body as plain text, with HTML tags removed."""
    plain_text = ""
    html_text = ""

    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in disposition.lower():
                continue
            if content_type not in {"text/plain", "text/html"}:
                continue

            payload = part.get_payload(decode=True)
            if payload is None:
                continue

            charset = part.get_content_charset() or "utf-8"
            content = decode_payload(payload, charset)
            if content_type == "text/html" and not html_text:
                html_text = content
            elif content_type == "text/plain" and not plain_text:
                plain_text = content
    else:
        payload = message.get_payload(decode=True)
        if payload is None:
            return ""
        charset = message.get_content_charset() or "utf-8"
        content = decode_payload(payload, charset)
        if message.get_content_type() == "text/html":
            return html_to_text(content)
        return normalize_plain_text(content)

    if html_text.strip():
        return html_to_text(html_text)
    if plain_text.strip():
        return normalize_plain_text(plain_text)
    return ""


def prepare_body_lines(body):
    lines = []
    for raw_line in body.splitlines():
        line = raw_line.replace("\xa0", " ")
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


def default_personalization_rules():
    return {
        "template": PERSONALIZATION_TEMPLATE,
        "aliases": {
            field: list(aliases)
            for field, aliases in PERSONALIZATION_ALIASES.items()
        },
        "cover_colour_values": sorted(COVER_COLOUR_VALUES),
        "foil_colour_values": sorted(FOIL_COLOUR_VALUES),
        "spine_text_hints": [
            "our",
            "wedding",
            "guest book",
            "story",
            "chapter",
            "forever",
            "love",
        ],
    }


def merge_personalization_rules(base, override):
    merged = {
        "template": base.get("template", PERSONALIZATION_TEMPLATE),
        "aliases": {
            field: list(values)
            for field, values in base.get("aliases", {}).items()
        },
        "cover_colour_values": list(
            base.get("cover_colour_values", COVER_COLOUR_VALUES)
        ),
        "foil_colour_values": list(
            base.get("foil_colour_values", FOIL_COLOUR_VALUES)
        ),
        "spine_text_hints": list(
            base.get("spine_text_hints", SPINE_TEXT_HINT_PATTERN.pattern)
        )
        if isinstance(base.get("spine_text_hints"), list)
        else [],
    }
    if not isinstance(override, dict):
        return merged
    for key in (
        "template",
        "cover_colour_values",
        "foil_colour_values",
        "spine_text_hints",
    ):
        if key in override:
            merged[key] = override[key]
    if isinstance(override.get("aliases"), dict):
        for field, values in override["aliases"].items():
            if isinstance(values, (list, tuple)):
                merged["aliases"][field] = list(values)
    return merged


def load_personalization_rules(shop_name, product_name):
    """Load rules by shop and product, with global defaults as fallback."""
    rules = default_personalization_rules()
    if not PERSONALIZATION_RULES_FILE.exists():
        return rules

    try:
        config = json.loads(
            PERSONALIZATION_RULES_FILE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        print(
            f"读取定制规则失败：{type(exc).__name__}: {exc}，使用内置默认规则。",
            flush=True,
        )
        return rules

    if not isinstance(config, dict):
        return rules
    rules = merge_personalization_rules(rules, config.get("default", {}))

    shop_map = config.get("shops", {})
    if not isinstance(shop_map, dict):
        return rules
    shop_candidates = [shop_name]
    shop_config = SHOP_MAP.get((shop_name or "").casefold())
    if shop_config:
        shop_candidates.append(shop_config["sheetName"])
        shop_candidates.append(shop_config["shopName"])

    selected_shop = None
    for candidate in shop_candidates:
        for configured_name, configured_rules in shop_map.items():
            if str(configured_name).casefold() == str(candidate).casefold():
                selected_shop = configured_rules
                break
        if selected_shop is not None:
            break
    if not isinstance(selected_shop, dict):
        return rules

    rules = merge_personalization_rules(rules, selected_shop.get("default", {}))
    products = selected_shop.get("products", {})
    if not isinstance(products, dict):
        return rules

    product_normalized = normalize_personalization_label(product_name or "")
    selected_product = None
    selected_length = -1
    for product_key, product_rules in products.items():
        if product_key == "*" or not isinstance(product_rules, dict):
            continue
        key_normalized = normalize_personalization_label(str(product_key))
        if key_normalized and key_normalized in product_normalized:
            if len(key_normalized) > selected_length:
                selected_product = product_rules
                selected_length = len(key_normalized)
    if selected_product is None and isinstance(products.get("*"), dict):
        selected_product = products["*"]
    return merge_personalization_rules(rules, selected_product or {})


def personalization_field_names(rules=None):
    template = (rules or default_personalization_rules()).get(
        "template",
        PERSONALIZATION_TEMPLATE,
    )
    if isinstance(template, (list, tuple)):
        return [str(field).strip() for field in template if str(field).strip()]

    fields = []
    for line in str(template).splitlines():
        match = re.match(r"^\s*\d+[.)]\s*(.+?)(?:\s+\(|\s*$)", line)
        if match:
            fields.append(match.group(1).strip())
    return fields


def normalize_personalization_label(value):
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def personalization_aliases(rules=None):
    rules = rules or default_personalization_rules()
    configured_aliases = rules.get("aliases", {})
    aliases = {}
    for field in personalization_field_names(rules):
        configured = configured_aliases.get(field, ())
        aliases[field] = tuple(dict.fromkeys((field, *configured)))
    return aliases


def extract_personalization_text(body):
    """Return only customer text between Personalization and Shop/Store."""
    lines = prepare_body_lines(body)
    start_pattern = re.compile(r"^Personalization\s*:?\s*(.*)$", re.IGNORECASE)
    stop_pattern = re.compile(r"^(?:Shop|Store)\s*:", re.IGNORECASE)
    collected = []
    collecting = False

    for line in lines:
        if not collecting:
            match = start_pattern.match(line)
            if not match:
                continue
            collecting = True
            inline_value = match.group(1).strip()
            if inline_value:
                collected.append(inline_value)
            continue

        if stop_pattern.match(line):
            break
        collected.append(line)

    return "\n".join(collected).strip()


def match_personalization_field(label, aliases):
    normalized_label = normalize_personalization_label(label)
    if not normalized_label:
        return ""

    scores = []
    for field, field_aliases in aliases.items():
        best_score = 0.0
        for alias in field_aliases:
            normalized_alias = normalize_personalization_label(alias)
            if normalized_label == normalized_alias:
                best_score = 1.0
                break
            if len(normalized_alias) >= 5 and normalized_alias in normalized_label:
                best_score = max(best_score, 0.92)
                continue
            if len(normalized_label) >= 4 and len(normalized_alias) >= 4:
                best_score = max(
                    best_score,
                    difflib.SequenceMatcher(
                        None,
                        normalized_label,
                        normalized_alias,
                    ).ratio(),
                )
        scores.append((best_score, field))

    scores.sort(reverse=True)
    best_score, best_field = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    if best_score >= 0.78 and best_score - second_score >= 0.08:
        return best_field
    return ""


def split_personalization_entries(text, aliases):
    alias_terms = sorted(
        {
            re.escape(alias)
            for field_aliases in aliases.values()
            for alias in field_aliases
        },
        key=len,
        reverse=True,
    )
    embedded_label = re.compile(
        r"(?<=[.!?])\s+(?=(?:\d+[.)]\s*)?(?:"
        + "|".join(alias_terms)
        + r")\s*(?:[:：=]|[-\u2013\u2014]))",
        re.IGNORECASE,
    )

    entries = []
    for line in text.splitlines():
        for part in re.split(r"\s*;\s*", line):
            entries.extend(
                piece.strip()
                for piece in embedded_label.split(part)
                if piece.strip()
            )
    return entries


def split_personalization_label(entry):
    entry = re.sub(r"^\s*\d+[.)]\s*", "", entry).strip()
    match = re.match(
        r"^(.{1,80}?)(?:\s*[:：=]\s*|\s*[-\u2013\u2014]\s*)(.*)$",
        entry,
    )
    if not match:
        return "", entry
    return match.group(1).strip(), match.group(2).strip()


def infer_personalization_field_from_value(entry, rules=None):
    """Infer a field only when an unlabeled value has a distinctive shape."""
    rules = rules or default_personalization_rules()
    value = entry.strip()
    normalized = normalize_personalization_label(value)

    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})", value):
        return "Date"

    if re.fullmatch(r"\+?\d[\d ()-]{6,}\d", value):
        return "Phone Number for Delivery"

    if re.fullmatch(r"#\s*\d{1,3}", value):
        return "Font Style"

    foil_colours = {
        normalize_personalization_label(item)
        for item in rules.get("foil_colour_values", FOIL_COLOUR_VALUES)
    }
    cover_colours = {
        normalize_personalization_label(item)
        for item in rules.get("cover_colour_values", COVER_COLOUR_VALUES)
    }
    if normalized in foil_colours:
        return "Foil Colour"

    if normalized in cover_colours:
        return "Cover Colour"

    name_parts = re.split(r"\s*(?:&|\+|\band\b)\s*", value, flags=re.IGNORECASE)
    if len(name_parts) == 2 and all(part.strip() for part in name_parts):
        normalized_parts = {
            normalize_personalization_label(part)
            for part in name_parts
        }
        known_colours = cover_colours | foil_colours
        if not normalized_parts.issubset(known_colours):
            return "Names on Cover"

    words = re.findall(r"[A-Za-z0-9']+", value)
    spine_hints = rules.get("spine_text_hints", [])
    spine_pattern = re.compile(
        r"\b(?:"
        + "|".join(re.escape(str(hint)) for hint in spine_hints if str(hint))
        + r")\b",
        re.IGNORECASE,
    ) if spine_hints else SPINE_TEXT_HINT_PATTERN
    if 2 <= len(words) <= 7 and spine_pattern.search(value):
        return "Spine Text"

    return ""


def looks_like_personalization_sentence(entry):
    """Distinguish prose from a compact sequence of personalization values."""
    reduced = re.sub(
        r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})(?!\d)",
        " ",
        entry,
    )
    reduced = re.sub(r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)", " ", reduced)
    reduced = re.sub(r"(?<!\w)#\s*\d{1,3}(?!\d)", " ", reduced)
    if re.search(r"[!?]|(?<!\d)\.(?:\s|$)", reduced):
        return True

    words = re.findall(r"[A-Za-z']+", reduced.casefold())
    if len(words) > 12:
        return True
    sentence_words = {
        "please",
        "would",
        "want",
        "like",
        "make",
        "put",
        "use",
        "using",
        "with",
        "for",
        "the",
        "on",
        "in",
        "at",
        "from",
        "should",
        "could",
        "can",
        "my",
        "your",
        "it",
        "is",
        "be",
    }
    return len(words) >= 5 and sum(word in sentence_words for word in words) >= 2


def infer_personalization_values(entry, rules=None):
    """Extract multiple distinctive unlabeled values from one input line."""
    rules = rules or default_personalization_rules()
    if looks_like_personalization_sentence(entry):
        return [], entry

    matches = []
    occupied = []

    def add_match(field, match):
        start, end = match.span()
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            return
        occupied.append((start, end))
        matches.append((start, field, match.group(0).strip()))

    for match in re.finditer(
        r"(?<!\d)\d{1,2}[./-]\d{1,2}[./-](?:\d{2}|\d{4})(?!\d)",
        entry,
    ):
        add_match("Date", match)

    for match in re.finditer(r"(?<!\w)\+?\d[\d ()-]{6,}\d(?!\w)", entry):
        add_match("Phone Number for Delivery", match)

    for match in re.finditer(r"(?<!\w)#\s*\d{1,3}(?!\d)", entry):
        add_match("Font Style", match)

    colour_groups = (
        ("Foil Colour", rules.get("foil_colour_values", FOIL_COLOUR_VALUES)),
        ("Cover Colour", rules.get("cover_colour_values", COVER_COLOUR_VALUES)),
    )
    for field, colours in colour_groups:
        for colour in sorted(colours, key=lambda item: len(str(item)), reverse=True):
            colour_pattern = re.escape(str(colour)).replace(r"\ ", r"\s+")
            for match in re.finditer(
                rf"(?<!\w){colour_pattern}(?!\w)",
                entry,
                flags=re.IGNORECASE,
            ):
                add_match(field, match)

    remaining_parts = list(entry)
    for start, end in occupied:
        remaining_parts[start:end] = " " * (end - start)
    remaining = re.sub(r"\s+", " ", "".join(remaining_parts)).strip(" ,;/")
    if remaining:
        remaining_field = infer_personalization_field_from_value(remaining, rules)
        if remaining_field:
            matches.append((len(entry), remaining_field, remaining))
            remaining = ""

    # Do not partially extract values from prose or an unknown custom message.
    if remaining:
        return [], entry

    matches.sort(key=lambda item: item[0])
    return [(field, value) for _, field, value in matches], remaining


def append_personalization_value(result, field, value):
    value = value.strip()
    if not value or field not in result:
        return
    if result[field]:
        result[field] += "\n" + value
    else:
        result[field] = value


def classify_personalization(text, rules=None):
    rules = rules or default_personalization_rules()
    fields = personalization_field_names(rules)
    result = {field: "" for field in fields}
    result["其他"] = ""
    if not text:
        return result

    aliases = personalization_aliases(rules)
    unmatched = []
    for entry in split_personalization_entries(text, aliases):
        label, value = split_personalization_label(entry)
        field = match_personalization_field(label, aliases) if label else ""

        if not field:
            # Accept an explicit field name at the beginning even without a
            # separator, but do not guess from a value such as a bare colour.
            for candidate, candidate_aliases in aliases.items():
                for alias in candidate_aliases:
                    match = re.match(
                        rf"^{re.escape(alias)}\b\s*(.*)$",
                        entry,
                        flags=re.IGNORECASE,
                    )
                    if match and match.group(1).strip():
                        field = candidate
                        value = match.group(1).strip(" :-=\t")
                        break
                if field:
                    break

        if not field:
            inferred_values, remaining = infer_personalization_values(entry, rules)
            if inferred_values:
                for inferred_field, inferred_value in inferred_values:
                    append_personalization_value(
                        result,
                        inferred_field,
                        inferred_value,
                    )
                if remaining:
                    unmatched.append(remaining)
                continue

        if not field:
            unmatched.append(entry)
            continue

        append_personalization_value(result, field, value or entry)

    result["其他"] = "\n".join(unmatched)
    return result


def find_first_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def extract_original_shop(body):
    return find_first_value(
        body,
        [
            r"^\s*Shop\s*:\s*([^\r\n]+)",
            r"^\s*Store\s*:\s*([^\r\n]+)",
        ],
    )


def extract_shop(body):
    shop_name = extract_original_shop(body)
    if not shop_name:
        return ""

    config = SHOP_MAP.get(shop_name.casefold())
    if not config:
        return shop_name
    return f'{config["sheetName"]}/{shop_name}'


def extract_order_number(subject, body):
    return find_first_value(
        subject + "\n" + body,
        [
            r"Your\s+order\s+number\s+is\s*[:：]?\s*#?\s*([0-9]{6,})",
            r"\bOrder\s*#\s*([A-Za-z0-9-]+)",
            r"^\s*Order\s*(?:number|no\.?)\s*:\s*([A-Za-z0-9-]+)",
            r"^\s*订单号\s*[:：]\s*([A-Za-z0-9-]+)",
        ],
    )


def extract_order_date(body, email_date):
    value = find_first_value(
        body,
        [
            r"^\s*Order date\s*:\s*([^\r\n]+)",
            r"^\s*Date ordered\s*:\s*([^\r\n]+)",
            r"^\s*Ordered on\s*:?\s*([^\r\n]+)",
            r"^\s*Purchase date\s*:\s*([^\r\n]+)",
            r"^\s*Sale date\s*:\s*([^\r\n]+)",
            r"^\s*订单日期\s*[:：]\s*([^\r\n]+)",
        ],
    )
    raw_date = value or email_date or ""
    if not raw_date:
        return ""

    try:
        parsed = parsedate_to_datetime(raw_date)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError):
        return raw_date


def extract_product_name(body):
    return find_first_value(
        body,
        [
            r"^\s*Product name\s*:\s*([^\r\n]+)",
            r"^\s*Item title\s*:\s*([^\r\n]+)",
            r"^\s*Product\s*:\s*([^\r\n]+)",
            r"^\s*Item\s*:\s*([^\r\n]+)",
            r"^\s*Listing title\s*:\s*([^\r\n]+)",
            r"^\s*Listing\s*:\s*([^\r\n]+)",
            r"^\s*产品名称\s*[:：]\s*([^\r\n]+)",
        ],
    )


def extract_quantity(body):
    value = find_first_value(
        body,
        [
            r"^\s*Quantity\s*:\s*(\d+)",
            r"^\s*Qty\s*:\s*(\d+)",
            r"^\s*Quantity\s+(\d+)\s*$",
            r"^\s*数量\s*[:：]\s*(\d+)",
        ],
    )
    return int(value) if value else ""


def extract_transaction_id(body):
    return find_first_value(
        body,
        [
            r"^\s*Transaction ID\s*:\s*([A-Za-z0-9-]+)",
            r"^\s*交易编号\s*[:：]\s*([A-Za-z0-9-]+)",
        ],
    )


def extract_item_price(body):
    return find_first_value(
        body,
        [
            r"^\s*Price\s*:\s*([^\r\n]+)",
            r"^\s*Item price\s*:\s*([^\r\n]+)",
            r"^\s*价格\s*[:：]\s*([^\r\n]+)",
        ],
    )


def format_transaction_line(line):
    """Format Etsy transaction labels into a stable text-column layout."""
    match = re.match(
        r"^(Transaction ID|Item|Photo size\s*&\s*Page color|"
        r"Quantity of pages\s*&\s*photos|Personalization|Quantity|"
        r"Item price)\s*:\s*(.*)$",
        line,
        re.IGNORECASE,
    )
    if not match:
        return line

    label = match.group(1).strip() + ":"
    value = match.group(2).strip()
    if label.casefold() in {
        "transaction id:",
        "item:",
        "quantity:",
        "item price:",
    }:
        return f"{label:<20}{value}".rstrip()
    return f"{label} {value}".rstrip()


def extract_order_options(body):
    lines = prepare_body_lines(body)
    transaction_start = re.compile(r"^Transaction ID\s*:", re.IGNORECASE)
    transaction_end = re.compile(r"^Item price\s*:", re.IGNORECASE)

    transaction_lines = []
    collecting = False
    for line in lines:
        if transaction_start.match(line):
            collecting = True
        if collecting:
            transaction_lines.append(format_transaction_line(line))
        if collecting and transaction_end.match(line):
            break

    if transaction_lines:
        return "\n".join(transaction_lines)

    option_pattern = re.compile(
        r"size|width|height|depth|dimension|box\s+width|box\s+depth|"
        r"page|inside|inner|paper|cover|personalization|personalisation|"
        r"custom|font|writing\s+color|colour|color|contact\s+phone|"
        r"phone\s+number|尺寸|页数|内页|定制|字体|颜色",
        re.IGNORECASE,
    )
    metadata_pattern = re.compile(
        r"^(Shop|Store|Order|Order date|Product|Item|Listing|Quantity|Qty)\s*:",
        re.IGNORECASE,
    )

    selected = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if option_pattern.search(line):
            selected.append(line)
            if line.endswith(":") and index + 1 < len(lines):
                next_line = lines[index + 1]
                if not metadata_pattern.match(next_line):
                    selected.append(next_line)
                    index += 1
        index += 1

    unique_lines = []
    seen = set()
    for line in selected:
        key = line.casefold()
        if key not in seen:
            seen.add(key)
            unique_lines.append(line)
    return "\n".join(unique_lines)


def find_line_index(lines, pattern, start=0):
    compiled = re.compile(pattern, re.IGNORECASE)
    for index in range(start, len(lines)):
        if compiled.search(lines[index]):
            return index
    return -1


def collect_section(lines, start_index, stop_patterns, max_lines=30):
    if start_index < 0:
        return []
    compiled_stops = [re.compile(pattern, re.IGNORECASE) for pattern in stop_patterns]
    section = []
    for line in lines[start_index:start_index + max_lines]:
        if section and any(pattern.search(line) for pattern in compiled_stops):
            break
        section.append(line)
    return section


def extract_payment_method_section(lines):
    start = find_line_index(lines, r"^Payment method\s*:?$")
    if start >= 0:
        section = collect_section(
            lines,
            start,
            [
                r"^Payments made via",
                r"^Shipping address\s*:?$",
                r"^Purchase Shipping Label",
                r"^Shipping internationally",
                r"^Sell with confidence",
                r"^Ship with DDP",
            ],
            max_lines=6,
        )
        return "\n".join(section).strip()

    paid_line = find_line_index(lines, r"^Paid via Etsy Payments\b")
    if paid_line < 0:
        return ""
    section = [lines[paid_line]]
    if not re.search(r"\b\d{4}\b", lines[paid_line]) and paid_line + 1 < len(lines):
        if re.fullmatch(r"\d{4}", lines[paid_line + 1]):
            section.append(lines[paid_line + 1])
    return "Payment method\n" + "\n".join(section)


def extract_shipping_address_section(lines):
    start = find_line_index(lines, r"^Shipping address\s*:?$")
    if start < 0:
        return ""
    section = collect_section(
        lines,
        start,
        [
            r"^Purchase Shipping Label",
            r"^Shipping internationally",
            r"^Sell with confidence",
            r"^Learn about Etsy Seller Protection",
            r"^Ship with DDP",
            r"^Order total\s*:?$",
        ],
        max_lines=10,
    )
    return "\n".join(section).strip()


def collapse_repeated_suffix(lines):
    """Collapse HTML/email clients that repeat the same product block."""
    for block_size in range(1, len(lines) // 2 + 1):
        if lines[-2 * block_size:-block_size] == lines[-block_size:]:
            return lines[-block_size:]
    return lines


def looks_like_product_title_continuation(line):
    """Identify an extra title line without treating prose as product text."""
    if line.endswith((".", "!", "?")):
        return False
    words = re.findall(r"[A-Za-z0-9]+", line)
    if not words or len(words) > 16:
        return False
    title_words = sum(
        1
        for word in words
        if word[0].isupper() or word.isdigit()
    )
    return title_words / len(words) >= 0.65


def extract_product_section(lines):
    personalization_index = find_line_index(
        lines,
        r"^Personalization(?:\s*:|\s*$)",
    )
    transaction_index = find_line_index(lines, r"^Transaction ID\s*:")
    end_index = personalization_index
    if end_index < 0:
        end_index = find_line_index(lines, r"^(?:Shop|Store)\s*:")
    if end_index < 0:
        end_index = transaction_index
    if end_index < 0:
        return ""

    attribute_pattern = re.compile(
        r"^(?:Book Size\s*\|\s*Page Count|Inside Page Options|"
        r"Photo size\s*&\s*Page color|Quantity of pages\s*&\s*photos|"
        r"Size|Style|Color|Colour|Cover|Font|Paper|Material|Page Count|"
        r"Pages|Option|Finish)\s*:",
        re.IGNORECASE,
    )
    noise_pattern = re.compile(
        r"^(?:Ship with DDP|With Delivered Duties Paid|Purchase Shipping Label|"
        r"Shipping internationally|Sell with confidence|Learn about|"
        r"Order details|Payment method|Shipping address|Shop|Store|"
        r"Paid via Etsy Payments|Payments made via)",
        re.IGNORECASE,
    )

    # Product variants sit immediately before Personalization. The linked Etsy
    # title can span multiple lines, so include adjacent title-like lines too.
    start = end_index - 1
    while start >= 0 and attribute_pattern.search(lines[start]):
        start -= 1
    if start >= 0 and not noise_pattern.search(lines[start]):
        title_start = start
        while title_start > 0:
            previous_line = lines[title_start - 1]
            if attribute_pattern.search(previous_line):
                break
            if noise_pattern.search(previous_line):
                break
            if not looks_like_product_title_continuation(previous_line):
                break
            title_start -= 1
        product_lines = lines[title_start:end_index]
    else:
        product_lines = lines[max(0, end_index - 1):end_index]

    return "\n".join(collapse_repeated_suffix(product_lines)).strip()


def extract_order_details(body):
    lines = prepare_body_lines(body)
    sections = (
        extract_payment_method_section(lines),
        extract_shipping_address_section(lines),
    )
    return "\n\n".join(section for section in sections if section)


def remove_section_heading(section, heading):
    lines = section.splitlines()
    if lines and re.fullmatch(rf"{re.escape(heading)}\s*:?", lines[0], re.IGNORECASE):
        lines = lines[1:]
    return "\n".join(lines).strip()


def extract_personalization_section(product_section):
    if not product_section:
        return ""
    selected = []
    pattern = re.compile(
        r"^(?:size|style|color|colour|personalization|personalisation|"
        r"photo size|quantity of pages|cover|font)\s*:|"
        r"^Personalized item\b|^No returns or exchanges accepted\b",
        re.IGNORECASE,
    )
    for line in product_section.splitlines():
        if pattern.search(line.strip()):
            selected.append(line.strip())
    return "\n".join(selected)


def extract_price_section(lines):
    start = find_line_index(lines, r"^Item total\s*:")
    if start < 0:
        start = find_line_index(lines, r"^Order total\s*:?$")
    if start < 0:
        return ""

    section = []
    price_label = re.compile(
        r"^(?:Item total|Discount|Subtotal|Shipping|Sales tax|Tax|Order total)\s*:",
        re.IGNORECASE,
    )
    money_value = re.compile(r"^-?\s*(?:[A-Z]{2,3}\s*)?[$€£¥]\s*[\d.,]+$")
    discount_note = re.compile(r"^The buyer applied these discounts\s*:", re.IGNORECASE)
    stop_pattern = re.compile(
        r"^(?:Contacting the buyer|Questions|Shop policies)\b",
        re.IGNORECASE,
    )

    for line in lines[start:start + 30]:
        if section and stop_pattern.search(line):
            break
        if price_label.search(line) or money_value.search(line) or discount_note.search(line):
            section.append(line)
        elif section and line:
            if section[-1].endswith(":"):
                section.append(line)
            elif discount_note.search(section[-1]):
                break
        if discount_note.search(line):
            break
    return "\n".join(section).strip()


def extract_grouped_order_fields(body):
    lines = prepare_body_lines(body)
    payment_method = extract_payment_method_section(lines)
    shipping_address = extract_shipping_address_section(lines)
    product_section = extract_product_section(lines)
    return {
        "订单详情": "\n\n".join(
            section for section in (payment_method, shipping_address) if section
        ),
        "商品信息": product_section,
        "个人定制信息": extract_personalization_section(product_section),
        "订单价格": extract_price_section(lines),
    }


def parse_order_fields(subject, body, email_date, metadata=None, uid=None):
    lines = prepare_body_lines(body)
    product_section = extract_product_section(lines)
    product_name = product_section or extract_product_name(body)
    personalization_text = extract_personalization_text(body)
    original_shop = extract_original_shop(body)
    personalization_rules = load_personalization_rules(
        original_shop,
        product_name,
    )
    payment_method = remove_section_heading(
        extract_payment_method_section(lines),
        "Payment method",
    )
    shipping_address = remove_section_heading(
        extract_shipping_address_section(lines),
        "Shipping address",
    )
    return {
        "订单号": extract_order_number(subject, body),
        "店铺": extract_shop(body),
        "产品": product_name,
        "定制信息": classify_personalization(
            personalization_text,
            personalization_rules,
        ),
        "付款方式": payment_method,
        "邮寄地址": shipping_address,
        "交易编号": extract_transaction_id(body),
        "数量": extract_quantity(body),
        "价格": extract_item_price(body),
    }


def post_order(order_data):
    if not APPS_SCRIPT_URL:
        print("未配置 APPS_SCRIPT_URL，本次只输出解析结果。", flush=True)
        return

    payload = dict(order_data)
    if WEBHOOK_TOKEN:
        payload["token"] = WEBHOOK_TOKEN

    response = requests.post(APPS_SCRIPT_URL, json=payload, timeout=30)
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Apps Script 返回的不是 JSON：{response.text[:500]}") from exc

    if not result.get("ok"):
        raise RuntimeError(f"Apps Script 写入失败：{result}")

    print(f"Google 表格写入成功：{result}", flush=True)


def load_last_uid():
    if not STATE_FILE.exists():
        return 0
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(data.get("last_uid", 0))
    except Exception:
        return 0


def save_last_uid(uid):
    STATE_FILE.write_text(
        json.dumps({"last_uid": uid}, ensure_ascii=False),
        encoding="utf-8",
    )


def process_new_messages(client, last_uid):
    all_uids = client.search(["ALL"])
    new_uids = sorted(uid for uid in all_uids if uid > last_uid)

    for uid in new_uids:
        try:
            header_data = client.fetch([uid], ["RFC822.HEADER"])[uid]
            raw_header = header_data.get(b"RFC822.HEADER") or header_data.get("RFC822.HEADER")
            if not raw_header:
                raise RuntimeError("无法读取邮件头")

            header_message = email.message_from_bytes(raw_header)
            subject = decode_header_value(header_message.get("Subject"))

            if not ORDER_PATTERN.search(subject):
                print(f"跳过 UID={uid}，主题：{subject}", flush=True)
                last_uid = uid
                save_last_uid(last_uid)
                continue

            print(
                f"\n收到新邮件：UID={uid}，主题：{subject}",
                flush=True,
            )

            full_data = client.fetch([uid], ["RFC822"])[uid]
            raw_message = full_data.get(b"RFC822") or full_data.get("RFC822")
            if not raw_message:
                raise RuntimeError("无法读取完整邮件")

            message = email.message_from_bytes(raw_message)
            body = extract_message_body(message)
            order_data = parse_order_fields(
                subject=subject,
                body=body,
                email_date=message.get("Date", ""),
                metadata={
                    "message_id": message.get("Message-ID", ""),
                    "from": decode_header_value(message.get("From", "")),
                    "reply_to": decode_header_value(message.get("Reply-To", "")),
                    "to": decode_header_value(message.get("To", "")),
                },
                uid=uid,
            )

            print("\n解析后的订单字段：", flush=True)
            print(json.dumps(order_data, ensure_ascii=False, indent=2), flush=True)
            post_order(order_data)

            last_uid = uid
            save_last_uid(last_uid)
            order_number = order_data.get("订单号") or "未识别"
            print(
                f"处理完成：UID={uid}，订单号={order_number}\n",
                flush=True,
            )

        except Exception as exc:
            print(
                f"处理 UID={uid} 失败：{type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
            # Keep the UID unchanged for real processing/write failures.
            break

    return last_uid


def connect_to_qq_mailbox():
    validate_config()
    print(f"[1] 连接 {IMAP_HOST}:{IMAP_PORT}", flush=True)
    client = IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True, timeout=30)
    print("[2] TLS 连接成功", flush=True)
    print("[3] 登录 QQ 邮箱", flush=True)
    try:
        client.login(QQ_USER, QQ_AUTH_CODE)
    except UnicodeEncodeError as exc:
        raise ConfigurationError(
            "IMAP 登录参数包含非 ASCII 字符。"
            "请使用真实 QQ 邮箱地址和 QQ 邮箱生成的英文/数字授权码，"
            "不要使用中文占位文字、全角引号或 QQ 登录密码。"
        ) from exc
    print("[4] 登录成功", flush=True)
    client.select_folder("INBOX", readonly=True)
    print("[5] 已进入 INBOX", flush=True)
    return client


def listen_forever():
    last_uid = load_last_uid()
    reconnect_delay = 5

    while True:
        client = None
        try:
            client = connect_to_qq_mailbox()

            if not STATE_FILE.exists():
                existing_uids = client.search(["ALL"])
                last_uid = max(existing_uids, default=0)
                save_last_uid(last_uid)
                print(f"首次运行，已跳过历史邮件，当前 UID={last_uid}", flush=True)

            print("开始监听 Etsy 成交通知新邮件……", flush=True)
            reconnect_delay = 5
            idle_round = 0

            while True:
                client.idle()
                try:
                    responses = client.idle_check(timeout=25)
                finally:
                    try:
                        client.idle_done()
                    except Exception:
                        pass

                idle_round += 1
                if responses:
                    print(f"邮箱状态变化：{responses}", flush=True)
                    last_uid = process_new_messages(client, last_uid)
                elif idle_round >= 4:
                    last_uid = process_new_messages(client, last_uid)
                    idle_round = 0

        except KeyboardInterrupt:
            print("\n程序已停止", flush=True)
            break
        except ConfigurationError:
            raise
        except Exception as exc:
            print(f"连接或监听异常：{type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        finally:
            if client is not None:
                try:
                    client.logout()
                except Exception:
                    pass

        print(f"{reconnect_delay} 秒后重新连接……", flush=True)
        time.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)


def validate_config():
    if not QQ_USER or not QQ_AUTH_CODE:
        raise ConfigurationError(
            "请在脚本顶部填写 QQ_USER 和 QQ_AUTH_CODE。\n"
            'QQ_USER = "123456789@qq.com"\n'
            'QQ_AUTH_CODE = "QQ邮箱生成的授权码"'
        )

    if not QQ_USER.isascii():
        raise ConfigurationError(
            "QQ_USER 包含中文或其他非 ASCII 字符。"
            "请把示例文字替换为真实 QQ 邮箱地址。"
        )

    if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", QQ_USER):
        raise ConfigurationError(f"QQ_USER 不是有效邮箱地址：{QQ_USER!r}")

    if not QQ_AUTH_CODE.isascii():
        raise ConfigurationError(
            "QQ_AUTH_CODE 包含中文或其他非 ASCII 字符。"
            "请填写 QQ 邮箱后台重新生成的真实授权码，不能填写示例文字。"
        )

    if not re.fullmatch(r"[A-Za-z0-9]+", QQ_AUTH_CODE):
        raise ConfigurationError(
            "QQ_AUTH_CODE 格式不正确。授权码只能包含英文字母和数字，"
            "不能包含空格、引号或中文。"
        )

    if APPS_SCRIPT_URL and not APPS_SCRIPT_URL.isascii():
        raise ConfigurationError(
            "APPS_SCRIPT_URL 包含中文。请使用 Apps Script 部署后真实的 /exec URL，"
            "不要使用示例占位文字。"
        )


def main():
    configure_utf8_console()
    socket.setdefaulttimeout(30)
    print(f"启动脚本：{Path(__file__).resolve()}", flush=True)
    try:
        validate_config()
        listen_forever()
    except ConfigurationError as exc:
        raise SystemExit(f"配置错误：{exc}") from None


if __name__ == "__main__":
    main()
