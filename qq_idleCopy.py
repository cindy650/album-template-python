import email
import difflib
import json
import os
import re
import socket
import sys
import tempfile
import time
import traceback
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import requests
from imapclient import IMAPClient


def _load_project_env():
    """Load local .env values before module-level service configuration."""
    env_path = Path(__file__).with_name(".env")
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_project_env()


# QQ IMAP configuration
IMAP_HOST = os.getenv("IMAP_HOST", "imap.qq.com").strip()
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
# QQ_AUTH_CODE is the IMAP authorization code generated in QQ Mail settings,
# not the password used to sign in to QQ.
QQ_USER = os.getenv("QQ_USER", "2051588081@qq.com").strip()
QQ_AUTH_CODE = os.getenv("QQ_AUTH_CODE", "qyxmdoobbbukfagb").strip()

# DeepSeek configuration. Secrets must be supplied through the environment or
# the project-local .env file; never commit an API key to source code.
DEEPSEEK_API_URL = os.getenv(
    "DEEPSEEK_API_URL",
    "https://api.deepseek.com/chat/completions",
).strip()
DEEPSEEK_API_KEY = os.getenv(
    "DEEPSEEK_API_KEY",
    "",
).strip()
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip()
DEEPSEEK_TIMEOUT = int(os.getenv("DEEPSEEK_TIMEOUT", "60"))

STATE_FILE = Path(__file__).with_name("qq_imap_state.json")
PERSONALIZATION_RULES_FILE = Path(__file__).with_name("personalization_rules.json")
ORDER_PATTERN = re.compile(
    r"You made a sale|sale on Etsy|Congratulations on your Etsy order",
    re.IGNORECASE,
)

PRODUCT_BLOCK_BOUNDARY_PATTERN = re.compile(
    r"^(?:Purchase Shipping Label|Shipping internationally|"
    r"Sell with confidence|Learn about|Ship with DDP|"
    r"With Delivered Duties Paid|Order details|Payment method|"
    r"Shipping address|Paid via Etsy Payments|Payments made via)",
    re.IGNORECASE,
)

PRODUCT_OPTION_LINE_PATTERN = re.compile(
    r"^(?P<label>[^:：]{1,100}?)\s*[:：]\s*(?P<value>.*)$"
)

RESERVED_PRODUCT_OPTION_LABELS = {
    "shop",
    "store",
    "transaction id",
    "transaction",
    "quantity",
    "qty",
    "price",
    "item price",
    "payment method",
    "shipping address",
}

SHOP_CONFIG = [
    {"displayName": "小彩灯", "shopName": "Memiya", "currency": "USD"},
    {"displayName": "3号店", "shopName": "LuxeJoy", "currency": "CAD"},
    {"displayName": "14号店", "shopName": "NuviaAlbum", "currency": "CAD"},
    {"displayName": "15号店", "shopName": "Mivow", "currency": "USD"},
    {"displayName": "16号店", "shopName": "Mmovia", "currency": "USD"},
    {"displayName": "17号店", "shopName": "ObiaMoment", "currency": "USD"},
    {"displayName": "18号店", "shopName": "18号店", "currency": "USD"},
]

SHOP_MAP = {
    item["shopName"].casefold(): item
    for item in SHOP_CONFIG
}


class ConfigurationError(ValueError):
    """A non-transient configuration problem that reconnecting cannot fix."""


class PersonalizationRuleNotFound(ConfigurationError):
    """No shop/product rule exists for one specific order."""


class ShopNotFoundError(ConfigurationError):
    """The Etsy shop from one email is absent from the system shop table."""


class ProductNotFoundError(ConfigurationError):
    """The Etsy product from one email is absent from the product table."""


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


def canonical_product_option_label(label):
    # Product option labels vary by listing; preserve the exact email label.
    return str(label or "").strip()


def split_product_option_line(line):
    match = PRODUCT_OPTION_LINE_PATTERN.match(line)
    if not match:
        return None
    label = match.group("label").strip()
    if label.casefold() in RESERVED_PRODUCT_OPTION_LABELS:
        return None
    if len(label.split()) > 16 or label.endswith((".", "!", "?")):
        return None
    return canonical_product_option_label(label), match.group("value").strip()


def find_product_option_span(lines):
    shop_index = find_line_index(lines, r"^(?:Shop|Store)\s*:")
    if shop_index < 0:
        return -1, -1
    search_start = 0
    for index in range(shop_index):
        if PRODUCT_BLOCK_BOUNDARY_PATTERN.search(lines[index]):
            search_start = index + 1
    for index in range(search_start, shop_index):
        if split_product_option_line(lines[index]) is not None:
            return index, shop_index
    return -1, shop_index


def extract_product_options_from_lines(lines):
    start_index, end_index = find_product_option_span(lines)
    if start_index < 0 or end_index < 0:
        return {}
    values = {}
    current_field = None
    for line in lines[start_index:end_index]:
        labeled = split_product_option_line(line)
        if labeled is not None:
            current_field, value = labeled
            values.setdefault(current_field, [])
            if value:
                values[current_field].append(value)
            continue
        if current_field:
            values[current_field].append(line)
    return {
        field: " ".join(parts).strip()
        for field, parts in values.items()
    }


def extract_product_options(body):
    return extract_product_options_from_lines(prepare_body_lines(body))


def read_personalization_config():
    if not PERSONALIZATION_RULES_FILE.exists():
        raise ConfigurationError(
            f"定制规则文件不存在：{PERSONALIZATION_RULES_FILE}"
        )
    try:
        config = json.loads(
            PERSONALIZATION_RULES_FILE.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ConfigurationError(
            f"读取定制规则失败：{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(config, dict):
        raise ConfigurationError("定制规则 JSON 的根节点必须是对象")
    if "default" in config:
        raise ConfigurationError("定制规则 JSON 不再支持根级 default")
    if not isinstance(config.get("shops"), dict) or not config["shops"]:
        raise ConfigurationError("定制规则 JSON 中的 shops 必须是非空对象")
    return config


def validate_personalization_rules(rules, location="商品规则"):
    if not isinstance(rules, dict):
        raise ConfigurationError(f"{location}必须是对象")
    template = rules.get("template")
    if not isinstance(template, list) or not personalization_field_names(rules):
        raise ConfigurationError(f"{location}中的 template 必须是非空数组")
    if not str(rules.get("prompt", "")).strip():
        raise ConfigurationError(f"{location}中的 prompt 不能为空")
    aliases = rules.get("aliases")
    if not isinstance(aliases, dict):
        raise ConfigurationError(f"{location}中的 aliases 必须是对象")
    for field, values in aliases.items():
        if not isinstance(values, list):
            raise ConfigurationError(
                f"{location}中的 aliases.{field} 必须是数组"
            )
    for key in (
        "cover_colour_values",
        "foil_colour_values",
        "spine_text_hints",
    ):
        if not isinstance(rules.get(key), list):
            raise ConfigurationError(f"{location}中的 {key} 必须是数组")
    return rules


def copy_personalization_rules(rules):
    return {
        "prompt": str(rules["prompt"]).strip(),
        "template": list(rules["template"]),
        "aliases": {
            str(field): list(values)
            for field, values in rules["aliases"].items()
        },
        "cover_colour_values": list(rules["cover_colour_values"]),
        "foil_colour_values": list(rules["foil_colour_values"]),
        "spine_text_hints": list(rules["spine_text_hints"]),
    }


def validate_personalization_config(config):
    for shop_name, shop_rules in config["shops"].items():
        location = f"shops.{shop_name}"
        if not isinstance(shop_rules, dict):
            raise ConfigurationError(f"{location} 必须是对象")
        if "default" in shop_rules:
            raise ConfigurationError(f"{location} 不再支持 default")
        products = shop_rules.get("products")
        if not isinstance(products, dict) or not products:
            raise ConfigurationError(f"{location}.products 必须是非空对象")
        if "*" in products:
            raise ConfigurationError(f"{location}.products 不再支持 * 通配规则")
        for product_name, product_rules in products.items():
            validate_personalization_rules(
                product_rules,
                f"{location}.products.{product_name}",
            )
    return config


def require_personalization_rules(rules):
    if rules is None:
        raise ConfigurationError(
            "必须先根据订单店铺和产品匹配 JSON 商品规则，才能分拣定制信息"
        )
    return rules


def load_personalization_rules(shop_name, product_name):
    """Require a shop match followed by a product match in the JSON file."""
    config = read_personalization_config()

    shop_map = config["shops"]
    shop_candidates = [shop_name]
    shop_config = SHOP_MAP.get((shop_name or "").casefold())
    if shop_config:
        shop_candidates.append(shop_config["displayName"])
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
        raise PersonalizationRuleNotFound(
            f"定制规则 JSON 中未找到店铺：{shop_name or '空店铺名'}"
        )

    products = selected_shop.get("products", {})
    if not isinstance(products, dict) or not products:
        raise ConfigurationError(f"店铺 {shop_name} 没有配置 products")

    product_normalized = normalize_personalization_label(product_name or "")
    selected_product = None
    selected_product_key = ""
    selected_length = -1
    for product_key, product_rules in products.items():
        if not isinstance(product_rules, dict):
            continue
        key_normalized = normalize_personalization_label(str(product_key))
        if key_normalized and key_normalized in product_normalized:
            if len(key_normalized) > selected_length:
                selected_product = product_rules
                selected_product_key = str(product_key)
                selected_length = len(key_normalized)
    if selected_product is None:
        raise PersonalizationRuleNotFound(
            f"店铺 {shop_name} 的定制规则中未找到产品："
            f"{product_name or '空产品名'}"
        )
    validate_personalization_rules(
        selected_product,
        f"shops.{shop_name}.products.{selected_product_key}",
    )
    return copy_personalization_rules(selected_product)


def personalization_field_names(rules=None):
    rules = require_personalization_rules(rules)
    template = rules.get("template", [])
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
    rules = require_personalization_rules(rules)
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
    rules = require_personalization_rules(rules)
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
        for item in rules.get("foil_colour_values", [])
    }
    cover_colours = {
        normalize_personalization_label(item)
        for item in rules.get("cover_colour_values", [])
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
    spine_pattern = None
    if spine_hints:
        spine_pattern = re.compile(
            r"\b(?:"
            + "|".join(
                re.escape(str(hint)) for hint in spine_hints if str(hint)
            )
            + r")\b",
            re.IGNORECASE,
        )
    if spine_pattern and 2 <= len(words) <= 7 and spine_pattern.search(value):
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
    rules = require_personalization_rules(rules)
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
        ("Foil Colour", rules.get("foil_colour_values", [])),
        ("Cover Colour", rules.get("cover_colour_values", [])),
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


def classify_personalization_locally(text, rules=None):
    rules = require_personalization_rules(rules)
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


def personalization_template_text(rules):
    """Return the listing prompt loaded from personalization_rules.json."""
    return str(rules.get("prompt", "")).strip()


def parse_deepseek_personalization_result(content, fields):
    """Validate the model JSON and return only the configured output fields."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("DeepSeek 返回内容为空")

    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("DeepSeek 返回的 JSON 不是对象")

    expected_fields = [*fields, "其他"]
    missing_fields = [field for field in expected_fields if field not in parsed]
    if missing_fields:
        raise ValueError(
            "DeepSeek 返回结果缺少字段：" + ", ".join(missing_fields)
        )

    result = {}
    for field in expected_fields:
        value = parsed[field]
        if not isinstance(value, str):
            raise ValueError(f"DeepSeek 字段 {field!r} 的值不是字符串")
        result[field] = value.strip()
    return result


def classify_personalization_with_deepseek(text, rules=None):
    """Use DeepSeek to map free-form customer text to listing fields."""
    rules = require_personalization_rules(rules)
    fields = personalization_field_names(rules)
    empty_result = {field: "" for field in (*fields, "其他")}
    if not text.strip():
        return empty_result
    if not DEEPSEEK_API_KEY:
        raise ConfigurationError("未配置 DEEPSEEK_API_KEY")

    system_prompt = """\
你是 Etsy 订单定制信息分类器。根据商家原本展示给客户的填写文案，把客户实际填写的原文归入指定字段。

严格遵守以下规则：
1. 只返回一个 JSON 对象，不要返回 Markdown、解释或额外字段。
2. JSON 必须包含所有指定字段以及“其他”；缺失字段使用空字符串。
3. 保留客户填写的值，不翻译、不改写、不纠错，也不要编造原文中不存在的信息。
4. 客户可能使用不同标签、打乱顺序、把多个值写在一行，或完全不写标签。应结合字段含义和值的形态匹配，而不是依赖顺序。
5. 去掉客户自己写的字段标签和分隔符，只把实际值放入对应字段。
6. 同一段原文只能归入一个字段。无法可靠匹配、与填写文案无关或含义模糊的内容放入“其他”。
7. 同一字段有多个值时，用换行连接，仍然返回字符串。
"""
    request_data = {
        "商家填写文案": personalization_template_text(rules),
        "必须返回的字段": [*fields, "其他"],
        "客户填写原文": text,
    }
    print("调用deepseek处理定制信息分拣", flush=True)
    response = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(request_data, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0,
        },
        timeout=DEEPSEEK_TIMEOUT,
    )
    response.raise_for_status()
    response_data = response.json()
    try:
        content = response_data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"DeepSeek 返回结构不正确：{response_data}") from exc
    result = parse_deepseek_personalization_result(content, fields)
    if not any(result.values()):
        raise ValueError("DeepSeek 未匹配任何定制信息")
    return result


def classify_personalization(text, rules=None):
    """Classify with DeepSeek, falling back locally if the API is unavailable."""
    rules = require_personalization_rules(rules)
    if not text.strip():
        return classify_personalization_locally(text, rules)
    try:
        return classify_personalization_with_deepseek(text, rules)
    except (requests.RequestException, ValueError, ConfigurationError) as exc:
        print(
            f"DeepSeek 定制信息匹配失败：{type(exc).__name__}: {exc}，"
            "改用本地规则匹配。",
            flush=True,
        )
        return classify_personalization_locally(text, rules)


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
    return extract_original_shop(body)


def extract_shop_name(body):
    shop = extract_original_shop(body)
    config = SHOP_MAP.get(shop.casefold()) if shop else None
    return config["displayName"] if config else ""


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
            r"^Transaction ID\s*:",
            r"^Quantity\s*:",
            r"^Qty\s*:",
            r"^Item price\s*:",
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
    option_start, _ = find_product_option_span(lines)
    if option_start > 0:
        title_start = option_start - 1
        while title_start > 0:
            previous_line = lines[title_start - 1]
            if PRODUCT_BLOCK_BOUNDARY_PATTERN.search(previous_line):
                break
            if not looks_like_product_title_continuation(previous_line):
                break
            title_start -= 1
        return " ".join(lines[title_start:option_start]).strip()

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


def extract_mail_order_identity(body):
    """Extract only the fields needed to decide whether an order is supported."""
    lines = prepare_body_lines(body)
    product_section = extract_product_section(lines)
    product_name = next(
        (
            line.strip()
            for line in product_section.splitlines()
            if line.strip()
        ),
        "",
    )
    if not product_name:
        product_name = extract_product_name(body)
    return {
        "original_shop": extract_original_shop(body),
        "shop": extract_shop(body),
        "shop_name": extract_shop_name(body),
        "product": product_name,
    }


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
        "商品信息": extract_product_options_from_lines(lines),
        "个人定制信息": extract_personalization_section(product_section),
        "订单价格": extract_price_section(lines),
    }


def parse_order_fields(
    subject,
    body,
    email_date,
    metadata=None,
    uid=None,
    shop_lookup=None,
):
    original_shop = extract_original_shop(body)
    shop = extract_shop(body)
    shop_name = extract_shop_name(body)
    if shop_lookup is not None and not shop_lookup(
        original_shop,
        shop_name,
    ):
        raise ShopNotFoundError(
            f"系统没有此店铺：{original_shop or shop_name or '空店铺名'}"
        )

    lines = prepare_body_lines(body)
    product_section = extract_product_section(lines)
    product_name = next(
        (
            line.strip()
            for line in product_section.splitlines()
            if line.strip()
        ),
        "",
    )
    if not product_name:
        product_name = extract_product_name(body)
    product_information = extract_product_options_from_lines(lines)
    payment_method = remove_section_heading(
        extract_payment_method_section(lines),
        "Payment method",
    )
    shipping_address = remove_section_heading(
        extract_shipping_address_section(lines),
        "Shipping address",
    )
    shipping_address = " ".join(shipping_address.splitlines())
    return {
        "订单号": extract_order_number(subject, body),
        "店铺": shop,
        "店铺名": shop_name,
        "产品": product_name,
        "商品信息": product_information,
        "付款方式": payment_method,
        "邮寄地址": shipping_address,
        "交易编号": extract_transaction_id(body),
        "数量": extract_quantity(body),
        "价格": extract_item_price(body),
    }


def load_last_uid():
    if not STATE_FILE.exists():
        return 0
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return int(data.get("last_uid", 0))
    except Exception:
        return 0


def save_last_uid(uid):
    payload = json.dumps({"last_uid": int(uid)}, ensure_ascii=False)
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    last_error = None
    for _ in range(3):
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=STATE_FILE.parent,
                prefix=f".{STATE_FILE.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, STATE_FILE)
            return True
        except OSError as exc:
            last_error = exc
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            time.sleep(0.05)
    print(
        f"保存邮箱 UID 状态失败，下一轮可能重复处理：{last_error}",
        flush=True,
    )
    return False


def dispatch_order(order_handler, order_data, uid=None, metadata=None):
    if order_handler is None:
        return order_data
    return order_handler(order_data, uid=uid, metadata=metadata or {})


def process_new_messages(
    client,
    last_uid,
    order_handler=None,
    shop_lookup=None,
    product_lookup=None,
):
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
            metadata = {
                "message_id": message.get("Message-ID", ""),
                "from": decode_header_value(message.get("From", "")),
                "reply_to": decode_header_value(message.get("Reply-To", "")),
                "to": decode_header_value(message.get("To", "")),
            }

            identity = extract_mail_order_identity(body)
            if shop_lookup is not None and not shop_lookup(
                identity["original_shop"],
                identity["shop_name"],
            ):
                raise ShopNotFoundError(
                    "系统没有此店铺："
                    f"{identity['original_shop'] or identity['shop_name'] or '空店铺名'}"
                )
            if product_lookup is not None and not product_lookup(
                identity["shop"],
                identity["shop_name"],
                identity["product"],
            ):
                raise ProductNotFoundError(
                    "店铺商品列表中没有匹配商品："
                    f"店铺={identity['shop'] or identity['shop_name'] or '空'}，"
                    f"商品={identity['product'] or '空'}"
                )

            order_data = parse_order_fields(
                subject=subject,
                body=body,
                email_date=message.get("Date", ""),
                metadata=metadata,
                uid=uid,
                # Shop and product were checked before full order parsing.
                shop_lookup=None,
            )

            print("\n解析后的订单字段：", flush=True)
            print(json.dumps(order_data, ensure_ascii=False, indent=2), flush=True)
            dispatch_order(
                order_handler,
                order_data,
                uid=uid,
                metadata=metadata,
            )

            last_uid = uid
            save_last_uid(last_uid)
            order_number = order_data.get("订单号") or "未识别"
            print(
                f"处理完成：UID={uid}，订单号={order_number}\n",
                flush=True,
            )

        except (ShopNotFoundError, ProductNotFoundError) as exc:
            print(
                f"跳过 UID={uid}：{exc}",
                flush=True,
            )
            last_uid = uid
            save_last_uid(last_uid)
            continue
        except PersonalizationRuleNotFound as exc:
            print(
                f"跳过 UID={uid}：{exc}",
                flush=True,
            )
            last_uid = uid
            save_last_uid(last_uid)
            continue
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


def stop_requested(stop_event):
    return stop_event is not None and stop_event.is_set()


def wait_for_stop(stop_event, timeout):
    if stop_event is None:
        time.sleep(timeout)
        return False
    return stop_event.wait(timeout)


def poll_mailbox_once(
    order_handler=None,
    shop_lookup=None,
    product_lookup=None,
):
    """Connect once, process all pending UIDs, then disconnect."""
    last_uid = load_last_uid()
    previous_uid = last_uid
    client = None
    try:
        client = connect_to_qq_mailbox()
        if not STATE_FILE.exists():
            existing_uids = client.search(["ALL"])
            last_uid = max(existing_uids, default=0)
            save_last_uid(last_uid)
            return {
                "initialized": True,
                "previous_uid": previous_uid,
                "last_uid": last_uid,
                "advanced": max(0, last_uid - previous_uid),
            }

        last_uid = process_new_messages(
            client,
            last_uid,
            order_handler,
            shop_lookup,
            product_lookup,
        )
        return {
            "initialized": False,
            "previous_uid": previous_uid,
            "last_uid": last_uid,
            "advanced": max(0, last_uid - previous_uid),
        }
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


def listen_forever(
    stop_event=None,
    order_handler=None,
    shop_lookup=None,
    product_lookup=None,
):
    last_uid = load_last_uid()
    reconnect_delay = 5

    while not stop_requested(stop_event):
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

            while not stop_requested(stop_event):
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
                    last_uid = process_new_messages(
                        client,
                        last_uid,
                        order_handler,
                        shop_lookup,
                        product_lookup,
                    )
                elif idle_round >= 4:
                    last_uid = process_new_messages(
                        client,
                        last_uid,
                        order_handler,
                        shop_lookup,
                        product_lookup,
                    )
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

        if stop_requested(stop_event):
            break
        print(f"{reconnect_delay} 秒后重新连接……", flush=True)
        if wait_for_stop(stop_event, reconnect_delay):
            break
        reconnect_delay = min(reconnect_delay * 2, 60)

    if stop_event is not None:
        print("邮箱监听任务已停止", flush=True)


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
