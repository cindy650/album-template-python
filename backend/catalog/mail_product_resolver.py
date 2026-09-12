from __future__ import annotations

from typing import Any
import json

import requests


def normalize_match_text(value: Any):
    return " ".join(str(value or "").split()).casefold()


class DeepSeekProductCategoryMatcher:
    """Translate a listing title and select a same-meaning product category."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        model: str = "deepseek-chat",
        timeout_seconds: float = 60,
    ):
        self.api_url = str(api_url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "deepseek-chat").strip()
        self.timeout_seconds = float(timeout_seconds)

    def match(self, product_name: str, candidates: list[dict[str, Any]]):
        if not self.api_url or not self.api_key:
            raise RuntimeError("未配置 DeepSeek，无法自动识别新商品所属产品")
        choices = [
            {
                "product_id": int(item["product_id"]),
                "name": str(item.get("name") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
            for item in candidates
            if item.get("product_id") is not None
            and str(item.get("name") or "").strip()
        ]
        if not choices:
            return None
        response = requests.post(
            self.api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是电商商品分类器。先把 listing_title 准确翻译为中文，"
                            "再判断它与候选产品分类是否表达同一种产品。只能从候选"
                            "product_id 中选择；仅主题、用途、材质或风格相近但产品类型"
                            "不同不得匹配。不确定时 matched_product_id 返回 null。"
                            "不要判断尺寸、页数或其他规格。只返回 JSON："
                            '{"translated_title":"","matched_product_id":null}。'
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "listing_title": product_name,
                                "product_categories": choices,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0,
            },
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        try:
            content = response.json()["choices"][0]["message"]["content"]
            payload = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError("DeepSeek 返回的产品分类 JSON 格式不正确") from exc
        matched_id = payload.get("matched_product_id")
        if matched_id is None:
            return None
        try:
            matched_id = int(matched_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("DeepSeek 返回的 matched_product_id 无效") from exc
        allowed = {item["product_id"] for item in choices}
        if matched_id not in allowed:
            raise ValueError("DeepSeek 返回了候选列表以外的产品 ID")
        return {
            "product_id": matched_id,
            "translated_title": str(payload.get("translated_title") or "").strip(),
        }


class MailProductResolver:
    def __init__(self, repository, semantic_matcher):
        self.repository = repository
        self.semantic_matcher = semantic_matcher

    def resolve(
        self,
        shop: str,
        shop_name: str,
        product_name: str,
        product_information: dict[str, Any],
    ):
        exact = self.repository.find_product_name_for_shop(product_name, shop, shop_name)
        if exact is not None:
            print(
                "[邮件分类] 已命中 product_names："
                f"商品={product_name or '空'}，"
                f"产品ID={exact.get('product_id') or '未知'}",
                flush=True,
            )
            return self._match_template_for_product(exact, product_information)
        print(
            "[邮件分类] 未命中已有 product_names："
            f"商品={product_name or '空'}",
            flush=True,
        )

        shop_product = self.repository.ensure_shop_product(
            product_name,
            shop,
            shop_name,
        )
        unresolved = {
            **(shop_product or {}),
            "product_name": product_name,
            "product_id": None,
            "size_template_id": None,
            "automatic_product_match": False,
        }
        print(
            "[邮件分类] 商品名已添加到当前店铺："
            f"店铺={((shop_product or {}).get('shop') or shop or shop_name or '空')}，"
            f"商品={product_name or '空'}，"
            f"结果={'新增' if (shop_product or {}).get('created') else '已存在'}",
            flush=True,
        )

        candidates = self.repository.list_mail_product_candidates(shop, shop_name)
        if not candidates:
            print(
                "[邮件分类] 当前店铺没有可用产品分类，"
                "订单继续入库并等待人工关联："
                f"店铺={shop or shop_name or '空'}，商品={product_name or '空'}",
                flush=True,
            )
            return unresolved
        identifier_matches = []
        info_keys = {normalize_match_text(key) for key in (product_information or {})}
        for item in candidates:
            identifiers = item.get("product_identifiers") or []
            if any(normalize_match_text(identifier) in info_keys for identifier in identifiers):
                identifier_matches.append(item)
        if len(identifier_matches) == 1:
            identified = self._match_template_for_product(
                identifier_matches[0], product_information
            )
            print(
                "[邮件分类] product_identifiers 命中产品："
                f"产品ID={identified.get('product_id') or '未知'}",
                flush=True,
            )
            return identified
        print(
            "[邮件分类] product_identifiers 匹配结果："
            f"命中产品数={len(identifier_matches)}，继续 DeepSeek",
            flush=True,
        )
        print(
            "[邮件分类] 开始 DeepSeek 翻译与产品分类："
            f"商品={product_name or '空'}，候选产品数={len(candidates)}",
            flush=True,
        )
        try:
            semantic_match = self.semantic_matcher.match(product_name, candidates)
        except Exception as exc:
            print(
                "[邮件分类] DeepSeek 翻译与产品分类失败："
                f"{type(exc).__name__}: {exc}；"
                "订单继续入库并等待人工关联",
                flush=True,
            )
            return unresolved
        if semantic_match is None:
            print(
                "[邮件分类] DeepSeek 分类完成，未匹配到对应产品，"
                "订单继续入库并等待人工关联："
                f"商品={product_name or '空'}",
                flush=True,
            )
            return unresolved
        candidate = next(
            (
                item
                for item in candidates
                if int(item["product_id"]) == semantic_match["product_id"]
            ),
            None,
        )
        if candidate is None:
            print(
                "[邮件分类] DeepSeek 返回的产品不在当前店铺候选中，"
                "订单继续入库并等待人工关联："
                f"产品ID={semantic_match.get('product_id') or '空'}",
                flush=True,
            )
            return unresolved
        print(
            "[邮件分类] DeepSeek 翻译与产品分类完成："
            f"译名={semantic_match.get('translated_title') or '空'}，"
            f"产品={candidate.get('name') or '空'}，"
            f"产品ID={candidate.get('product_id') or '未知'}",
            flush=True,
        )
        specification_field = str(candidate.get("specification_field") or "").strip()
        marker_field = str(candidate.get("template_marker") or "").strip()
        lookup_field = specification_field or marker_field
        specification_value = self._field_value(product_information, lookup_field)
        print(
            "[邮件分类] 读取订单规格字段："
            f"字段={specification_field or '空'}，"
            f"字段值={specification_value or '空'}",
            flush=True,
        )
        normalized_value = normalize_match_text(specification_value)
        if not specification_field:
            template_marker_matcher = getattr(
                self.repository, "find_product_template_marker", None
            )
            template_match = (
                template_marker_matcher(
                    candidate["product_id"], candidate["shop_id"], specification_value
                )
                if template_marker_matcher is not None
                else None
            )
            matched_specification = (
                template_match.get("matched_marker") if template_match else None
            )
            print(
                "[邮件分类] 使用 template_marker 匹配模板："
                f"字段={marker_field or '空'}，字段值={specification_value or '空'}，"
                f"结果={'唯一命中' if template_match else '未唯一命中'}",
                flush=True,
            )
            configured_specifications = []
        else:
            template_match = None
        configured_specifications = candidate.get("specifications") or []
        if specification_field and configured_specifications:
            matched_specification = next(
                (
                    value
                    for value in sorted(
                        configured_specifications,
                        key=lambda item: len(normalize_match_text(item)),
                        reverse=True,
                    )
                    if normalize_match_text(value)
                    and normalize_match_text(value) in normalized_value
                ),
                None,
            )
            template_match = None
        elif specification_field:
            print(
                "[邮件分类] 当前产品未配置 specifications，改用关联模板规格列表判断："
                f"产品={candidate.get('name') or '空'}",
                flush=True,
            )
            template_matcher = getattr(
                self.repository,
                "find_product_template_specification",
                None,
            )
            template_match = (
                template_matcher(
                    candidate["product_id"],
                    candidate["shop_id"],
                    specification_value,
                )
                if template_matcher is not None
                else None
            )
            matched_specification = (
                template_match.get("matched_specification")
                if template_match
                else None
            )
        print(
            "[邮件分类] specifications 包含判断："
            f"可选规格={configured_specifications or '使用模板规格列表'}，"
            f"命中规格={matched_specification or '未命中'}",
            flush=True,
        )
        if not specification_value or matched_specification is None:
            print(
                "[邮件分类] 规格不匹配，订单继续入库并等待人工关联："
                f"商品={product_name or '空'}",
                flush=True,
            )
            return unresolved
        print(
            "[邮件分类] 正在写入 product_names："
            f"产品ID={candidate.get('product_id') or '未知'}，"
            f"商品={product_name or '空'}",
            flush=True,
        )
        associated = self.repository.associate_mail_product_name(
            candidate["product_id"],
            candidate["shop_id"],
            product_name,
        )
        associated["translated_product_name"] = semantic_match.get(
            "translated_title", ""
        )
        associated["matched_specification"] = matched_specification
        associated["specification_value"] = specification_value
        if template_match:
            associated["size_template_id"] = template_match.get("size_template_id")
        print(
            "[邮件分类] product_names 写入成功："
            f"产品ID={candidate.get('product_id') or '未知'}，"
            f"商品={product_name or '空'}",
            flush=True,
        )
        return associated

    @staticmethod
    def _field_value(product_information: dict[str, Any], field_name: str):
        expected = normalize_match_text(field_name)
        for key, value in (product_information or {}).items():
            if normalize_match_text(key) == expected:
                return " ".join(str(value or "").split())
        return ""

    def _match_template_for_product(self, product: dict[str, Any], product_information):
        # The repository may include the first template as a legacy fallback;
        # automatic matching must replace it only after a unique rule match.
        product = {**product, "size_template_id": None}
        if not product.get("template_marker") and not product.get("specification_field"):
            try:
                with self.repository.connect() as connection:
                    row = connection.execute(
                        "SELECT template_marker FROM products WHERE id = ?",
                        (product["product_id"],),
                    ).fetchone()
                if row:
                    product["template_marker"] = row["template_marker"]
            except Exception:
                pass
        specification_field = str(product.get("specification_field") or "").strip()
        marker_field = str(product.get("template_marker") or "").strip()
        field = specification_field or marker_field
        value = self._field_value(product_information, field)
        if not value:
            return product
        if marker_field:
            marker_matcher = getattr(self.repository, "find_product_template_marker", None)
            marker_match = marker_matcher(product["product_id"], product["shop_id"], value) if marker_matcher else None
            if not marker_match:
                return product
            product = {**product, "size_template_id": marker_match["size_template_id"]}
            if specification_field:
                spec_value = self._field_value(product_information, specification_field)
                spec_matcher = getattr(self.repository, "find_template_specification", None)
                if spec_matcher and not spec_matcher(product["size_template_id"], spec_value):
                    return {**product, "size_template_id": None}
            return product
        if specification_field:
            matcher = getattr(self.repository, "find_product_template_specification", None)
            match = matcher(product["product_id"], product["shop_id"], value) if matcher else None
        else:
            matcher = getattr(self.repository, "find_product_template_marker", None)
            match = matcher(product["product_id"], product["shop_id"], value) if matcher else None
        if match:
            product = {**product, "size_template_id": match.get("size_template_id")}
        return product
