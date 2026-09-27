import json

import watcher as m
from watcher import ProductStatus, pick_shop as pick


def reserved_html(stock):
    product = {"name": "Tanga s výšivkou", "price": 199.0, "sizes": [
        {"sizeId": 1035, "sizeName": "XS", "isInStock": False, "stockQuantity": 0},
        {"sizeId": 1036, "sizeName": "S", "isInStock": False, "stockQuantity": 0},
        {"sizeId": 1037, "sizeName": "M", "isInStock": False, "stockQuantity": 0},
    ]}
    return ("<html><script>\nfunction initStickyHeader(){ if (typeof window?.getProductData === 'function') {return;} }\n"
            "window['getProductData'] = function() {\n            return " + json.dumps(product, ensure_ascii=False) +
            ";\n        };\n window['getStockData'] = function() {\n            return " + json.dumps(stock) +
            ";\n        };\n</script></html>")


def test_reserved_out_of_stock():
    st = m.lpp_parse(reserved_html({"1035": 0, "1036": 0, "1037": 0}), "https://www.reserved.com/cz/cz/x")
    assert st.name == "Tanga s výšivkou"
    assert st.sizes == {"XS": False, "S": False, "M": False}


def test_reserved_restock_of_m():
    st = m.lpp_parse(reserved_html({"1035": 0, "1036": 0, "1037": 3}), "u")
    assert st.available_sizes() == ["M"]


def flight(chunks):
    return "".join('<script>self.__next_f.push([1,%s])</script>' % json.dumps(c) for c in chunks)


def dec_sku(size, model, avail, group="334419"):
    return ('{"skuId":"abc-%s","title":"Boty","sizeLabel":"%s","offers":[{"x":1}],"itemGroupId":"%s",'
            '"modelId":"%s","isHomeOrStoreDeliveryAvailable":%s,"isAvailable":%s}' %
            (size, size, group, model, str(avail).lower(), str(avail).lower()))


URL = "https://www.decathlon.cz/p/damske-boty/_/R-p-334419?mc=8914038"


def test_decathlon_only_36_and_ignores_recommendations():
    text = ('1:{"itemGroup":{"skuGroups":[{"skus":[' + dec_sku("36", "8914038", True) + ']}]}}\n'
            '2:{"reco":[' + dec_sku("S", "999", True, "111") + "," + dec_sku("38", "777", True, "222") + ']}')
    html = "<title>Dámské boty MT Cushion 2 | Decathlon</title>" + flight([text[:120], text[120:]])
    st = m.decathlon_parse(html, URL)
    assert st.sizes == {"36": True}
    assert st.name == "Dámské boty MT Cushion 2"


def test_decathlon_new_size_appears():
    text = '[' + ",".join([dec_sku("36", "8914038", True), dec_sku("38", "8914038", True),
                           dec_sku("39", "8914038", False)]) + ']'
    st = m.decathlon_parse(flight([text]), URL)
    assert st.sizes == {"36": True, "38": True, "39": False}
    item = {"ignore_sizes": ["36"]}
    assert m.matching(item, st)[0] == ["38"]


def test_generic_jsonld_variants():
    ld = {"@context": "https://schema.org", "@type": "ProductGroup", "name": "Tričko", "hasVariant": [
        {"@type": "Product", "size": "S", "offers": {"availability": "https://schema.org/OutOfStock"}},
        {"@type": "Product", "size": "M", "offers": {"availability": "https://schema.org/InStock"}},
    ]}
    html = '<script type="application/ld+json">%s</script>' % json.dumps(ld)
    st = m.generic_parse(html, "https://shop.example/x")
    assert st.sizes == {"S": False, "M": True}
    assert st.product_available is True


def test_generic_product_level():
    ld = {"@type": "Product", "name": "Hrnek", "offers": [{"availability": "http://schema.org/OutOfStock"}]}
    st = m.generic_parse('<script type="application/ld+json">%s</script>' % json.dumps(ld), "u")
    assert st.sizes == {} and st.product_available is False


def test_pick():
    assert pick("https://www.reserved.com/cz/cz/x")[1] is m.lpp_parse
    assert pick("https://www.sinsay.com/cz/cs/x")[1] is m.lpp_parse
    assert pick("https://www.decathlon.cz/p/x")[1] is m.decathlon_parse
    assert pick("https://www.zalando.cz/x")[1] is m.generic_parse


def test_matching_rules():
    st = ProductStatus(sizes={"XS": True, "S": False, "M": True, "L": True})
    assert m.matching({"watch_sizes": ["s", "m"]}, st)[0] == ["M"]
    assert m.matching({"ignore_sizes": ["M"]}, st)[0] == ["XS", "L"]
    assert m.matching({}, st)[0] == ["XS", "M", "L"]
    assert "XXL" in m.matching({"watch_sizes": ["XXL"]}, st)[1]


def test_run_notifies_only_on_transition(tmp_path, monkeypatch):
    items = {"items": [{"id": "a", "name": "Tanga", "url": "https://www.reserved.com/x", "watch_sizes": ["S", "M"]}]}
    (tmp_path / "items.json").write_text(json.dumps(items))
    monkeypatch.setattr(m, "ITEMS", tmp_path / "items.json")
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    sent = []
    monkeypatch.setattr(m, "send_message", lambda text, dry=False: sent.append(text) or ["ok"])
    stocks = iter([{"1036": 0, "1037": 0}, {"1036": 0, "1037": 2}, {"1036": 0, "1037": 2}, {"1036": 1, "1037": 2}])
    monkeypatch.setattr(m, "get_html", lambda url: reserved_html(next(stocks)))

    m.run(); assert sent == []
    m.run(); assert len(sent) == 1 and "Velikost: M" in sent[0]
    m.run(); assert len(sent) == 1           # still in stock -> no spam
    m.run(); assert len(sent) == 2 and "Velikost: S" in sent[1]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["items"]["a"]["matched"] == ["M", "S"]


def test_error_alert_after_threshold(tmp_path, monkeypatch):
    from watcher import FetchError
    (tmp_path / "items.json").write_text(json.dumps({"items": [{"id": "a", "url": "https://www.reserved.com/x"}]}))
    monkeypatch.setattr(m, "ITEMS", tmp_path / "items.json")
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    sent = []
    monkeypatch.setattr(m, "send_message", lambda text, dry=False: sent.append(text) or ["ok"])

    def boom(url):
        raise FetchError("blocked")
    monkeypatch.setattr(m, "get_html", boom)
    for _ in range(m.ERROR_ALERT_AFTER + 2):
        m.run()
    assert len(sent) == 1 and "⚠️" in sent[0]
