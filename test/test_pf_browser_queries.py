"""pf_browser 列表查询条件。"""

from product_feed_kr.pf_browser.queries import _looks_like_image_hash, _where_clause


def test_where_q_numeric_includes_exact_id_and_goods_num() -> None:
    sql, params = _where_clause(album_id=None, q="5244152")
    assert "id = ?" in sql
    assert "TRIM(commodity_goods_num) = ?" in sql
    assert params[0] == 5244152
    assert params[1] == "5244152"
    assert "CAST(id AS TEXT) LIKE ?" in sql


def test_where_q_numeric_goods_num_exact() -> None:
    sql, params = _where_clause(album_id=None, q="258757")
    assert "TRIM(commodity_goods_num) = ?" in sql
    assert params[1] == "258757"
    assert "commodity_goods_num LIKE ?" in sql


def test_where_q_text_includes_id_cast_like() -> None:
    sql, params = _where_clause(album_id=None, q="abc")
    assert "id = ?" not in sql
    assert "CAST(id AS TEXT) LIKE ?" in sql
    assert "first_image_hash LIKE ?" in sql
    assert params[-1] == "%abc%"


def test_where_q_image_hash_exact() -> None:
    h = "a1b2c3d4e5f6789012345678901234567890abcd"
    assert _looks_like_image_hash(h)
    sql, params = _where_clause(album_id=None, q=h)
    assert "LOWER(TRIM(first_image_hash)) = LOWER(?)" in sql
    assert params[0] == h
    assert "first_image_hash LIKE ?" in sql
