"""标题人民币价解析：仅 💰 与 P 数字。"""

from product_feed_kr.wego.wego_commodity import price_from_title_cny

BRUNELLO_TITLE = (
    "P650 BC新品板鞋 Brunel*o Cucinell* 新款运动鞋男鞋出货！"
    "官方售价RMB ¥ 257,500   此品牌是来自意大利的世界顶级奢侈品牌"
)


def test_p650_ignores_official_yuan_retail() -> None:
    assert price_from_title_cny(BRUNELLO_TITLE) == "650"


def test_yuan_and_rmb_never_matched() -> None:
    assert price_from_title_cny("正品 官方售价 ¥ 1,280 包邮") is None
    assert price_from_title_cny("官方售价RMB ¥ 257,500") is None
    assert price_from_title_cny("人民币 999") is None


def test_money_bag() -> None:
    assert price_from_title_cny("💰335 短裤") == "335"


def test_money_bag_before_p_in_title() -> None:
    assert price_from_title_cny("💰290 P650 说明") == "290"


def test_p_price_not_after_letter() -> None:
    assert price_from_title_cny("GP270 外套") is None
    assert price_from_title_cny("拿货 P270 外套") == "270"
