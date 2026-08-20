# -*- coding: utf-8 -*-
"""
stack_detector.py — Wappalyzer-lite fingerprinting без сети.

Определяет технологический стек сайта по уже собранным данным
(site_data: html + headers + meta + tech_headers), с фокусом на
российский рынок. Версии извлекаются, где это видно.

Публичный интерфейс:
    async def detect_stack(site_data: dict) -> dict
        -> {"technologies": {category: [names]}, "flat": [unique names], "backend_hint": str}

Никогда не бросает исключения наружу: при любой ошибке возвращает
{"technologies": {}, "flat": [], "backend_hint": ""}.
"""

import re

# Безопасный результат по умолчанию
_SAFE_DEFAULT = {"technologies": {}, "flat": [], "backend_hint": ""}


def _s(val) -> str:
    """Привести значение к строке безопасно."""
    try:
        if val is None:
            return ""
        if isinstance(val, bytes):
            return val.decode("utf-8", "ignore")
        return str(val)
    except Exception:
        return ""


def _lower_dict(d) -> dict:
    """Вернуть dict со строковыми lower-ключами и строковыми значениями."""
    out = {}
    try:
        if not isinstance(d, dict):
            return out
        for k, v in d.items():
            try:
                key = _s(k).lower()
                out[key] = _s(v)
            except Exception:
                continue
    except Exception:
        return {}
    return out


def _first_group(pattern, text, flags=re.IGNORECASE, group=1):
    """Найти первую группу regex или вернуть None."""
    try:
        m = re.search(pattern, text, flags)
        if m:
            g = m.group(group)
            if g:
                return g.strip()
    except Exception:
        return None
    return None


def detect_stack_sync(site_data: dict) -> dict:
    """
    Синхронное ядро детекции (вся логика тут; detect_stack — тонкая async-обёртка).
    """
    try:
        if not isinstance(site_data, dict):
            return {"technologies": {}, "flat": [], "backend_hint": ""}

        html = _s(site_data.get("html"))
        text = _s(site_data.get("text"))
        headers = _lower_dict(site_data.get("headers"))
        tech_headers = _lower_dict(site_data.get("tech_headers"))
        meta = site_data.get("meta") if isinstance(site_data.get("meta"), dict) else {}

        # Общий "haystack" для строчного поиска (нижний регистр)
        html_l = html.lower()
        # Всё вместе для широких строковых сигнатур
        blob = html_l + " " + text.lower()

        generator = _s(meta.get("generator")) if isinstance(meta, dict) else ""
        generator_l = generator.lower()

        # Объединённые заголовки (headers + tech_headers), tech_headers имеет доп.вес
        all_headers = {}
        all_headers.update(headers)
        all_headers.update(tech_headers)

        server_hdr = all_headers.get("server", "")
        powered_by = all_headers.get("x-powered-by", "")

        # Собранные категории: category -> list[name] (без дублей, порядок добавления)
        cats = {}

        def add(category, name):
            try:
                if not name:
                    return
                lst = cats.setdefault(category, [])
                if name not in lst:
                    lst.append(name)
            except Exception:
                pass

        # ---------------------------------------------------------------
        # CMS
        # ---------------------------------------------------------------
        # WordPress + версия из meta generator
        if ("wp-content" in html_l or "wp-json" in html_l or "wp-includes" in html_l
                or "wordpress" in generator_l):
            ver = _first_group(r"wordpress[\s/]*([\d]+\.[\d.]+)", generator_l)
            add("cms", "WordPress " + ver if ver else "WordPress")

        # 1C-Bitrix
        if ("/bitrix/" in html_l or "bx." in html_l
                or re.search(r"\bbitrix\b", blob) or "1c-bitrix" in generator_l):
            # Битрикс24 — отдельный продукт/виджет
            if "bitrix24" in blob or "b24" in html_l or "cdn-ru.bitrix24" in html_l:
                add("cms", "Битрикс24")
            add("cms", "1C-Bitrix")

        # Joomla
        if "joomla" in blob or "joomla" in generator_l or "/components/com_" in html_l:
            ver = _first_group(r"joomla![\s/]*([\d]+\.[\d.]+)", generator_l)
            add("cms", "Joomla " + ver if ver else "Joomla")

        # Drupal
        if ("drupal" in generator_l or "drupal-settings-json" in html_l
                or "/sites/default/files" in html_l or "data-drupal" in html_l):
            ver = _first_group(r"drupal[\s/]*([\d]+(?:\.[\d.]+)?)", generator_l)
            add("cms", "Drupal " + ver if ver else "Drupal")

        # Tilda
        if "tilda" in blob or "tildacdn" in html_l:
            add("cms", "Tilda")

        # Wix
        if "wix.com" in html_l or "wixstatic" in html_l or "x-wix" in " ".join(all_headers.keys()):
            add("cms", "Wix")

        # MODX
        if "modx" in blob or "modx" in generator_l:
            add("cms", "MODX")

        # OpenCart (в CMS и в ecommerce)
        if "opencart" in blob or "route=product" in html_l or "index.php?route=" in html_l:
            add("cms", "OpenCart")

        # ---------------------------------------------------------------
        # FRAMEWORK (frontend/backend)
        # ---------------------------------------------------------------
        # Next.js
        if "__next_data__" in html_l or "/_next/" in html_l:
            add("framework", "Next.js")
        # Nuxt
        if "__nuxt__" in html_l or "/_nuxt/" in html_l:
            add("framework", "Nuxt")
        # React
        if ("data-reactroot" in html_l or "react-dom" in html_l
                or "_reactrootcontainer" in html_l or "react.production" in html_l):
            add("framework", "React")
        # Vue
        if "data-v-" in html_l or "__vue__" in html_l or "vue.js" in html_l or "vue.min.js" in html_l:
            add("framework", "Vue")
        # Angular
        if "ng-version" in html_l or "ng-app" in html_l or "angular.js" in html_l:
            ver = _first_group(r'ng-version=["\']([\d.]+)', html)
            add("framework", "Angular " + ver if ver else "Angular")
        # Laravel (по cookie/заголовкам)
        set_cookie = all_headers.get("set-cookie", "").lower()
        if ("laravel_session" in set_cookie or "laravel_session" in html_l
                or "xsrf-token" in set_cookie):
            add("framework", "Laravel")
        # Django
        if ("csrftoken" in set_cookie or "csrfmiddlewaretoken" in html_l
                or "__admin_media_prefix__" in html_l):
            add("framework", "Django")
        # Ruby on Rails
        if ("csrf-param" in html_l or "authenticity_token" in html_l
                or "_rails" in set_cookie):
            add("framework", "Ruby on Rails")
        # ASP.NET
        if ("__viewstate" in html_l or "asp.net_sessionid" in set_cookie
                or "aspnet" in powered_by.lower() or "asp.net" in powered_by.lower()):
            add("framework", "ASP.NET")

        # ---------------------------------------------------------------
        # FRONTEND (библиотеки)
        # ---------------------------------------------------------------
        # jQuery + версия
        if "jquery" in html_l:
            ver = _first_group(r"jquery[.\-]?(?:ui[.\-])?v?([\d]+\.[\d]+(?:\.[\d]+)?)", html_l)
            if not ver:
                ver = _first_group(r"jquery/([\d]+\.[\d]+(?:\.[\d]+)?)", html_l)
            add("frontend", "jQuery " + ver if ver else "jQuery")
        # Bootstrap
        if "bootstrap" in html_l:
            ver = _first_group(r"bootstrap[.\-/]?v?([\d]+\.[\d]+(?:\.[\d]+)?)", html_l)
            add("frontend", "Bootstrap " + ver if ver else "Bootstrap")
        # Tailwind
        if "tailwind" in html_l or re.search(r'class="[^"]*\b(?:flex|grid)\b[^"]*\b(?:gap-|px-|py-|text-)', html_l):
            if "tailwind" in html_l:
                add("frontend", "Tailwind")

        # ---------------------------------------------------------------
        # ANALYTICS
        # ---------------------------------------------------------------
        if ("google-analytics.com" in html_l or "gtag(" in html_l
                or "googletagmanager.com/gtag" in html_l or "ga('create'" in html_l
                or "www.googletagmanager.com/gtag/js" in html_l):
            add("analytics", "Google Analytics")
        if "googletagmanager.com/gtm" in html_l or "gtm-" in html_l or "datalayer" in html_l:
            add("analytics", "Google Tag Manager")
        if ("mc.yandex.ru" in html_l or "ym(" in html_l or "yandex_metrika" in html_l
                or "yandexmetrika" in html_l or re.search(r"\bmetrika\b", html_l)):
            add("analytics", "Yandex Metrika")
        if ("connect.facebook.net" in html_l and "fbq(" in html_l) or "fbq('init'" in html_l or "facebook pixel" in blob:
            add("analytics", "Facebook Pixel")
        if "vk.com/rtrg" in html_l or "vk-pixel" in html_l or "_tmr" in html_l and "vk" in html_l:
            add("analytics", "VK Pixel")
        if "top-fwz1.mail.ru" in html_l or "top.mail.ru" in html_l or "_tmr.push" in html_l:
            add("analytics", "Mail.ru top")
        if "roistat" in html_l:
            add("analytics", "Roistat")

        # ---------------------------------------------------------------
        # CHAT WIDGET
        # ---------------------------------------------------------------
        if "jivosite" in html_l or "jivo" in html_l or "code.jivosite.com" in html_l:
            add("chat_widget", "JivoSite")
        if "bitrix24" in html_l or "b24" in html_l or "cdn-ru.bitrix24" in html_l or "b24-widget" in html_l:
            add("chat_widget", "Bitrix24")
        if "talk-me" in html_l or "talkme" in html_l or "lib.talk-me" in html_l:
            add("chat_widget", "Talk-Me")
        if "carrotquest" in html_l or "carrot quest" in blob or "cdn.carrotquest" in html_l:
            add("chat_widget", "Carrot quest")
        if "intercom" in html_l or "widget.intercom.io" in html_l:
            add("chat_widget", "Intercom")
        if "tawk.to" in html_l or "embed.tawk.to" in html_l:
            add("chat_widget", "Tawk.to")
        if "chatra" in html_l or "call.chatra.io" in html_l:
            add("chat_widget", "Chatra")
        if "envybox" in html_l or "envy" in html_l and "box" in html_l:
            if "envybox" in html_l:
                add("chat_widget", "Envybox")
        if "callibri" in html_l:
            add("chat_widget", "Callibri")

        # ---------------------------------------------------------------
        # ECOMMERCE
        # ---------------------------------------------------------------
        if "woocommerce" in html_l or "wc-block" in html_l or "wp-content/plugins/woocommerce" in html_l:
            add("ecommerce", "WooCommerce")
        if "insales" in html_l or "static.insales" in html_l or "insales-cdn" in html_l:
            add("ecommerce", "InSales")
        if "shopify" in html_l or "cdn.shopify.com" in html_l or "myshopify.com" in html_l:
            add("ecommerce", "Shopify")
        if "magento" in blob or "/static/version" in html_l or "mage/" in html_l or "magento" in generator_l:
            add("ecommerce", "Magento")
        if "cs-cart" in html_l or "cscart" in html_l or "cs cart" in blob:
            add("ecommerce", "CS-Cart")
        if "opencart" in blob or "route=product" in html_l:
            add("ecommerce", "OpenCart")
        # Bitrix eshop — если есть Битрикс + признаки магазина
        if ("1C-Bitrix" in cats.get("cms", []) and
                ("/catalog/" in html_l or "basket" in html_l or "sale_order" in html_l
                 or "bx-basket" in html_l or "catalog.section" in html_l)):
            add("ecommerce", "Bitrix eshop")

        # ---------------------------------------------------------------
        # CDN
        # ---------------------------------------------------------------
        header_keys_join = " ".join(all_headers.keys())
        cf_ray = all_headers.get("cf-ray", "")
        if (cf_ray or "cf-ray" in header_keys_join or "__cf" in html_l
                or "cloudflare" in server_hdr.lower()
                or "cloudflare" in all_headers.get("cf-cache-status", "").lower()
                or "cf-cache-status" in header_keys_join):
            add("cdn", "Cloudflare")
        if "cdn.jsdelivr.net" in html_l:
            add("cdn", "jsDelivr")
        if "unpkg.com" in html_l:
            add("cdn", "unpkg")
        if "yandex" in html_l and ("cdn" in html_l or "yastatic" in html_l):
            if "yastatic.net" in html_l or "yandex-cdn" in html_l or "storage.yandexcloud" in html_l:
                add("cdn", "Yandex CDN")

        # ---------------------------------------------------------------
        # SERVER (из заголовков)
        # ---------------------------------------------------------------
        srv_l = server_hdr.lower()
        pb_l = powered_by.lower()
        if "nginx" in srv_l:
            ver = _first_group(r"nginx/([\d.]+)", srv_l)
            add("server", "nginx/" + ver if ver else "nginx")
        if "apache" in srv_l:
            ver = _first_group(r"apache/([\d.]+)", srv_l)
            add("server", "Apache/" + ver if ver else "Apache")
        if "iis" in srv_l or "microsoft-iis" in srv_l:
            ver = _first_group(r"iis/([\d.]+)", srv_l)
            add("server", "IIS/" + ver if ver else "IIS")
        # PHP (из server или x-powered-by)
        php_ver = (_first_group(r"php/([\d.]+)", pb_l)
                   or _first_group(r"php/([\d.]+)", srv_l))
        if php_ver:
            add("server", "PHP/" + php_ver)
        elif "php" in pb_l:
            add("server", "PHP")
        # Express
        if "express" in pb_l:
            add("server", "Express")

        # ---------------------------------------------------------------
        # PAYMENTS
        # ---------------------------------------------------------------
        if ("yookassa" in blob or "yoomoney" in html_l or "kassa.yandex" in html_l
                or "money.yandex" in html_l or "яндекс.касса" in blob or "юкасса" in blob):
            add("payments", "YooKassa")
        if "cloudpayments" in html_l or "widget.cloudpayments" in html_l:
            add("payments", "CloudPayments")
        if "robokassa" in blob or "auth.robokassa" in html_l:
            add("payments", "Robokassa")
        if ("sberbank" in blob and ("acquiring" in blob or "payment" in blob or "sbrf" in html_l)) \
                or "securepayments.sberbank" in html_l or "3dsec.sberbank" in html_l:
            add("payments", "Сбербанк acquiring")
        if "tinkoff" in blob or "securepay.tinkoff" in html_l or "acdc.tinkoff" in html_l:
            add("payments", "Tinkoff")
        if "js.stripe.com" in html_l or "stripe.com/v3" in html_l or "stripe" in html_l and "checkout" in html_l:
            if "stripe" in html_l:
                add("payments", "Stripe")
        if "paypal.com" in html_l or "paypalobjects" in html_l or "paypal" in blob:
            if "paypal" in html_l:
                add("payments", "PayPal")

        # ---------------------------------------------------------------
        # Сборка результата
        # ---------------------------------------------------------------
        technologies = {}
        for category, names in cats.items():
            # Уникализируем, сохраняя порядок
            seen = []
            for n in names:
                if n and n not in seen:
                    seen.append(n)
            if seen:
                technologies[category] = seen

        # flat — отсортированный уникальный список всех имён
        flat_set = set()
        for names in technologies.values():
            for n in names:
                flat_set.add(n)
        flat = sorted(flat_set, key=lambda s: s.lower())

        # backend_hint — единственная лучшая догадка о бэкенде
        # Приоритет сигналов: cms -> framework -> server
        backend_hint = ""
        try:
            if technologies.get("cms"):
                backend_hint = technologies["cms"][0]
            elif technologies.get("framework"):
                # Предпочитаем именно backend-фреймворки, если они есть
                backend_frameworks = ("Laravel", "Django", "Ruby on Rails", "ASP.NET")
                chosen = None
                for name in technologies["framework"]:
                    base = name.split(" ")[0]
                    if any(name.startswith(bf) or base == bf.split(" ")[0] for bf in backend_frameworks):
                        chosen = name
                        break
                backend_hint = chosen or technologies["framework"][0]
            elif technologies.get("server"):
                backend_hint = technologies["server"][0]
        except Exception:
            backend_hint = ""

        return {
            "technologies": technologies,
            "flat": flat,
            "backend_hint": backend_hint or "",
        }

    except Exception:
        return {"technologies": {}, "flat": [], "backend_hint": ""}


async def detect_stack(site_data: dict) -> dict:
    """
    Определить технологический стек сайта (Wappalyzer-lite, без сети).

    Аргументы:
        site_data: dict, возвращаемый scraper.scrape_site (ключи: html,
                   headers, meta, tech_headers, text и т.д.).

    Возвращает:
        {
            "technologies": {category: [names]},  # пустые категории опущены
            "flat": [unique names, sorted],
            "backend_hint": str                    # лучшая догадка о бэкенде
        }

    Никогда не бросает исключения: при ошибке ->
        {"technologies": {}, "flat": [], "backend_hint": ""}.
    """
    try:
        return detect_stack_sync(site_data)
    except Exception:
        return {"technologies": {}, "flat": [], "backend_hint": ""}
