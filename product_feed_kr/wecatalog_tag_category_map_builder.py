"""从结构化列表生成 wecatalog_tag_category_map.json。

用法（在仓库根目录）::
    python -m product_feed_kr.wecatalog_tag_category_map_builder
    python -m product_feed_kr.wecatalog_tag_category_map_builder -v --log-file data/mapbuild.log

后续新增分类：在 RAW_ROWS 末尾追加一行，再运行本脚本。
每行可为 (group, tag, \"세그먼트 > ...\", anchor_only) 或末尾加可选第五项 tag_id（整数）：
写入 JSON meta `tag_id`，爬虫用其与列表 `tags[].tagId` 对齐；同名标签在不同分组时建议填写。
anchor_only=True 表示该行仅用于独立站目录锚点（该分组下无商品挂在主标签上）。

重新生成 JSON 时，会保留现有文件中已填的 **`meta.seven17_ca_id`**（seven17 后台分类），避免被覆盖；
也可直接在 **`wecatalog_tag_category_map.json`** 里手写该字段。
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from product_feed_kr.pf_log import configure_module_logging, pf_kv

_log = logging.getLogger(__name__)

# (分组名, 标签名, 路径 \"a > b\", 是否锚点, [可选 tag_id])
RAW_ROWS: list[tuple[str, str, str, bool] | tuple[str, str, str, bool, int]] = [
    # --- 1 男性服装 ---
    ("男性服装", "男性服装", "의류 > 남성의류", True),
    ("男性服装", "Chrome Hearts Men's Clothing", "의류 > 남성의류 > 크롬하츠", False),
    ("男性服装", "Burberry Men", "의류 > 남성의류 > 버버리", False),
    ("男性服装", "prada 男装", "의류 > 남성의류 > 프라다", False),
    ("男性服装", "Balenciaga 男装", "의류 > 남성의류 > 발렌시아가", False),
    ("男性服装", "Polo Ralph Lauren", "의류 > 남성의류 > 폴로 랄프 로렌", False),
    ("男性服装", "Stone Island", "의류 > 남성의류 > 스톤 아일랜드", False),
    ("男性服装", "ARC'TERYX （综合）", "아웃도어 > 아크테릭스 > 옷", False),
    ("男性服装", "kiton（衣服）", "의류 > 남성의류 > 키톤", False),
    ("男性服装", "杰尼亚", "의류 > 남성의류 > 제냐", False),
    ("男性服装", "路易威登（男装）", "의류 > 남성의류 > 루이비통", False),
    ("男性服装", "GUCCI（男装）", "의류 > 남성의류 > 구찌", False),
    ("男性服装", "셀린", "의류 > 남성의류 > 셀린느", False),
    ("男性服装", "Custom Leather Jacket", "의류 > 남성의류", False),
    ("男性服装", "코트", "의류 > 남성의류", False),
    ("男性服装", "청바지", "의류 > 남성의류", False),
    ("男性服装", "Moncler", "의류 > 남성의류", False),
    ("男性服装", "브루넬로 쿠치넬리Brunello Cucinelli", "의류 > 남성의류 > 브루넬로 쿠치넬리", False),
    ("男性服装", "톰브라운Thom Browne", "의류 > 남성의류 > 톰 브라운", False),
    ("男性服装", "톰 포드 TF", "의류 > 남성의류 > 톰 포드", False),
    ("男性服装", "로로피아나 (남성)Loro Piana", "의류 > 남성의류 > 로로피아나", False),
    ("男性服装", "디올(남성)디오", "의류 > 남성의류 > 디올", False),
    # --- 2 女鞋专区 ---
    ("女鞋专区", "女鞋专区", "신발 > 럭셔리 슈즈 (여성)", True),
    ("女鞋专区", "Hermès women's shoes", "신발 > 럭셔리 슈즈 (여성) > 에르메스", False),
    ("女鞋专区", "Roger Vivier 女鞋", "신발 > 럭셔리 슈즈 (여성) > 로저비베", False),
    ("女鞋专区", "Christian Louboutin 女鞋", "신발 > 럭셔리 슈즈 (여성) > 크리스찬 루부탱", False),
    ("女鞋专区", "Jimmy Choo 女鞋", "신발 > 럭셔리 슈즈 (여성) > 지미추", False),
    ("女鞋专区", "Loro Piana（女鞋）", "신발 > 럭셔리 슈즈 (여성) > 로로피아나", False),
    ("女鞋专区", "Prada women's shoes", "신발 > 럭셔리 슈즈 (여성) > 프라다", False),
    ("女鞋专区", "罗意威（女鞋）", "신발 > 럭셔리 슈즈 (여성) > 로에베", False),
    ("女鞋专区", "FENDI", "신발 > 럭셔리 슈즈 (여성) > 펜디", False),
    ("女鞋专区", "MIUMIU", "신발 > 럭셔리 슈즈 (여성) > 미우미우", False),
    ("女鞋专区", "샤넬", "신발 > 럭셔리 슈즈 (여성) > 샤넬", False),
    ("女鞋专区", "구찌 (여성용)", "신발 > 럭셔리 슈즈 (여성) > 구찌", False),
    # --- 3 Belt专区 ---
    ("Belt专区", "Belt专区", "벨트", True),
    ("Belt专区", "BV Belt", "벨트 > 발렌시아가", False),
    ("Belt专区", "Celine Belt", "벨트 > 셀린느", False),
    ("Belt专区", "Coach Belt", "벨트 > 코치", False),
    ("Belt专区", "Dior Belt", "벨트 > 디올", False),
    ("Belt专区", "Fendi Belt", "벨트 > 펜디", False),
    ("Belt专区", "Ferragamo Belt", "벨트 > 페라가모", False),
    ("Belt专区", "Gucci Belt", "벨트 > 구찌", False),
    ("Belt专区", "Hermès Belt", "벨트 > 에르메스", False),
    ("Belt专区", "Loewe Belt", "벨트 > 로에베", False),
    ("Belt专区", "LV Belt", "벨트 > 루이비통", False),
    ("Belt专区", "Montblanc Belt", "벨트 > 몽블랑", False),
    ("Belt专区", "Prada Belt", "벨트 > 프라다", False),
    ("Belt专区", "SR Belt", "벨트 > 스테파노 리치", False),
    ("Belt专区", "Tom Ford Belt", "벨트 > 톰 포드", False),
    ("Belt专区", "Versace Belt", "벨트 > 베르사체", False),
    ("Belt专区", "YSL Belt", "벨트 > 생로랑", False),
    ("Belt专区", "Zegna Belt", "벨트 > 제냐", False),
    ("Belt专区", "Chanel Belt", "벨트 > 샤넬", False),
    # --- 4 Luxury Shoes Corner ---
    ("Luxury Shoes Corner", "Luxury Shoes Corner", "신발 > 럭셔리 슈즈 (남성)", True),
    ("Luxury Shoes Corner", "Brunello Cucinelli", "신발 > 럭셔리 슈즈 (남성) > 브루넬로 쿠치넬리", False),
    ("Luxury Shoes Corner", "布鲁提（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 브루티", False),
    ("Luxury Shoes Corner", "Henderson Baracco", "신발 > 럭셔리 슈즈 (남성) > 헨더슨 바라코", False),
    ("Luxury Shoes Corner", "汤姆布朗（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 톰 브라운", False),
    ("Luxury Shoes Corner", "芬迪（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 펜디", False),
    ("Luxury Shoes Corner", "香奈儿（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 샤넬", False),
    ("Luxury Shoes Corner", "Zilli（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 질리", False),
    ("Luxury Shoes Corner", "纪梵希（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 지방시", False),
    ("Luxury Shoes Corner", "圣罗兰（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 생로랑", False),
    ("Luxury Shoes Corner", "罗意威（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 로에베", False),
    ("Luxury Shoes Corner", "巴黎世家", "신발 > 럭셔리 슈즈 (남성) > 발렌시아가", False),
    ("Luxury Shoes Corner", "MIHARA YASUHIRO", "신발 > 럭셔리 슈즈 (남성)", False),
    ("Luxury Shoes Corner", "Maison Margiela MM6", "신발 > 럭셔리 슈즈 (남성)", False),
    ("Luxury Shoes Corner", "华伦天奴", "신발 > 럭셔리 슈즈 (남성) > 발렌티노", False),
    ("Luxury Shoes Corner", "蒙口", "신발 > 럭셔리 슈즈 (남성)", False),
    ("Luxury Shoes Corner", "Kiton", "신발 > 럭셔리 슈즈 (남성) > 키톤", False),
    ("Luxury Shoes Corner", "路易威登（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 루이비통", False),
    ("Luxury Shoes Corner", "Ferragamo", "신발 > 럭셔리 슈즈 (남성) > 페라가모", False),
    ("Luxury Shoes Corner", "爱马仕（男鞋）", "신발 > 럭셔리 슈즈 (남성) > 에르메스", False),
    ("Luxury Shoes Corner", "TOMFORD", "신발 > 럭셔리 슈즈 (남성) > 톰 포드", False),
    ("Luxury Shoes Corner", "GGDB", "신발 > 럭셔리 슈즈 (남성)", False),
    ("Luxury Shoes Corner", "프라다 (남성)普拉达 (男性)", "신발 > 럭셔리 슈즈 (남성) > 프라다", False),
    ("Luxury Shoes Corner", "구찌(남성)古驰", "신발 > 럭셔리 슈즈 (남성) > 구찌", False),
    ("Luxury Shoes Corner", "디올(남성 신발)", "신발 > 럭셔리 슈즈 (남성) > 디올", False),
    ("Luxury Shoes Corner", "TOD.S", "신발 > 럭셔리 슈즈 (남성) > 토즈", False),
    # --- 5 女士包专区 ---
    ("女士包专区", "女士包专区", "가방", True),
    ("女士包专区", "Bottega Veneta（女包）", "가방 > 보테가 베네타", False),
    ("女士包专区", "Goyard", "가방 > 고야드", False),
    ("女士包专区", "巴黎世家（包包）", "가방 > 발렌시아가", False),
    ("女士包专区", "버버리", "가방 > 버버리", False),
    ("女士包专区", "圣罗兰", "가방 > 생로랑", False),
    ("女士包专区", "罗意威", "가방 > 로에베", False),
    ("女士包专区", "바렌티노", "가방 > 발렌티노", False),
    ("女士包专区", "芬迪", "가방 > 펜디", False),
    ("女士包专区", "赛琳", "가방 > 셀린느", False),
    ("女士包专区", "Chloé", "가방 > 끌로에", False),
    ("女士包专区", "爱马仕", "가방 > 에르메스", False),
    ("女士包专区", "미우미우", "가방 > 미우미우", False),
    ("女士包专区", "香奈儿", "가방 > 샤넬", False),
    ("女士包专区", "구찌", "가방 > 구찌", False),
    ("女士包专区", "루이비통", "가방 > 루이비통", False),
    ("女士包专区", "普拉达", "가방 > 프라다", False),
    # --- 6 手表专区 ---
    ("手表专区", "手表专区", "시계", True),
    ("手表专区", "Franck Muller", "시계 > 프랭크 뮬러", False),
    ("手表专区", "卡地亚（AF공장）", "시계 > 까르띠에 (AF공장)", False),
    ("手表专区", "Roger Dubuis", "시계 > 로저 두비", False),
    ("手表专区", "江诗丹顿", "시계 > 바쉐론 콘스탄틴", False),
    ("手表专区", "欧米茄（系列）", "시계 > 오메가", False),
    ("手表专区", "HUBLOT（系列）", "시계 > 휴블럿", False),
    ("手表专区", "IWC（系列）", "시계 > IWC", False),
    ("手表专区", "劳力士（V3顶级版本）", "시계 > 롤렉스", False),
    ("手表专区", "理查德米勒（顶级版本）", "시계 > 리처드 밀", False),
    ("手表专区", "沛纳海（顶级版本）", "시계 > 파네라이", False),
    ("手表专区", "爱彼（系列）", "시계 > 오데마 피게", False),
    # --- 7 儿童专区 ---
    ("儿童专区", "儿童专区", "키즈", True),
    ("儿童专区", "LV（童鞋）", "키즈 > 루이비통", False),
    ("儿童专区", "Alo Yoga（童鞋）", "키즈 > 알로 요가", False),
    ("儿童专区", "麦昆（童鞋）", "키즈 > 맥퀸", False),
    ("儿童专区", "巴黎世家（童鞋）", "키즈 > 발렌시아가", False),
    ("儿童专区", "Yeezy Boost 350（童鞋）", "키즈 > 이지 부스트", False),
    ("儿童专区", "the north face（儿童羽绒服）", "키즈 > 노스 페이스", False),
    # --- 8 装饰品 ---
    ("装饰品", "装饰品", "잡화", True),
    ("装饰品", "手机壳", "잡화 > 휴대폰 케이스", False),
    ("装饰品", "钥匙扣", "잡화 > 키링", False),
    ("装饰品", "摆件", "잡화 > 장식품", False),
    # --- 9 New Balance专区 ---
    ("New Balance专区", "New Balance专区", "신발 > 운동화 > 뉴발란스", True),
    ("New Balance专区", "204", "신발 > 운동화 > 뉴발란스 > 뉴발란스 204", False),
    ("New Balance专区", "860", "신발 > 운동화 > 뉴발란스 > 뉴발란스 860", False),
    ("New Balance专区", "2002", "신발 > 운동화 > 뉴발란스 > 뉴발란스 2002", False),
    ("New Balance专区", "740", "신발 > 운동화 > 뉴발란스 > 뉴발란스 740", False),
    ("New Balance专区", "1000", "신발 > 운동화 > 뉴발란스 > 뉴발란스 1000", False),
    ("New Balance专区", "991", "신발 > 운동화 > 뉴발란스 > 뉴발란스 991", False),
    ("New Balance专区", "992", "신발 > 운동화 > 뉴발란스 > 뉴발란스 992", False),
    ("New Balance专区", "9060", "신발 > 운동화 > 뉴발란스 > 뉴발란스 9060", False),
    ("New Balance专区", "U2000", "신발 > 운동화 > 뉴발란스 > 뉴발란스 U2000", False),
    ("New Balance专区", "1906", "신발 > 운동화 > 뉴발란스 > 뉴발란스 1906", False),
    # --- 10 户外 ---
    ("户外", "户外", "아웃도어", True),
    ("户外", "萨洛蒙", "아웃도어 > 살로몬", False),
    ("户外", "巴塔哥尼亚", "아웃도어 > 파타고니아", False),
    ("户外", "天伯伦", "아웃도어 > 팀버랜드", False),
    ("户外", "哥伦比亚", "아웃도어 > 콜롬비아", False),
    ("户外", "Arc'teryx（衣服）", "아웃도어 > 아크테릭스 > 옷", False),
    ("户外", "北面（衣服）", "아웃도어 > 노스페이스 > 옷", False),
    ("户外", "北面（鞋子）", "아웃도어 > 노스페이스 > 신발", False),
    ("户外", "Arc'teryx（鞋）", "아웃도어 > 아크테릭스 > 신발", False),
    # --- 11 香水专区 ---
    ("香水专区", "香水专区", "향수", True),
    ("香水专区", "GUCCI(香水)", "향수 > 구찌", False),
    ("香水专区", "Penhaligon's", "향수 > 펜할리곤스", False),
    ("香水专区", "Creed", "향수 > 크리드", False),
    ("香水专区", "Parfums de Marly", "향수 > 퍼퓸 드 말리", False),
    ("香水专区", "Tom Ford（香水）", "향수 > 톰 포드", False),
    ("香水专区", "Frederic Malle", "향수 > 프레데릭 말", False),
    ("香水专区", "Maison Margiela", "향수 > 메종 마르지엘라", False),
    ("香水专区", "Diptyque", "향수 > 딥티크", False),
    ("香水专区", "VERSACE（香水）", "향수 > 베르사체", False),
    ("香水专区", "宝格丽（香水）", "향수 > 불가리", False),
    ("香水专区", "LE LABO（香水）", "향수 > 르 라보", False),
    ("香水专区", "圣罗兰（香水）", "향수 > 입생로랑", False),
    ("香水专区", "바이레도(BYREDO)", "향수 > 바이레도", False),
    ("香水专区", "阿玛尼（香水）", "향수 > 아르마니", False),
    ("香水专区", "迪奥（香水）", "향수 > 디올", False),
    ("香水专区", "香奈儿（香水）", "향수 > 샤넬", False),
    # --- 12 乔丹 ---
    ("乔丹", "乔丹", "신발 > 운동화 > 조던", True),
    ("乔丹", "AJ8", "신발 > 운동화 > 조던 > 에어 조던 8", False),
    ("乔丹", "AJ13", "신발 > 운동화 > 조던 > 에어 조던 13", False),
    ("乔丹", "AJ1 high", "신발 > 운동화 > 조던 > 에어 조던 1 하이", False),
    ("乔丹", "AJ1 LOW", "신발 > 운동화 > 조던 > 에어 조던 1 로우", False),
    ("乔丹", "AJ1", "신발 > 운동화 > 조던 > 에어 조던 1", False),
    ("乔丹", "AJ3", "신발 > 운동화 > 조던 > 에어 조던 3", False),
    ("乔丹", "AJ12", "신발 > 운동화 > 조던 > 에어 조던 12", False),
    ("乔丹", "AJ11", "신발 > 운동화 > 조던 > 에어 조던 11", False),
    ("乔丹", "AJ4", "신발 > 운동화 > 조던 > 에어 조던 4", False),
    ("乔丹", "AJ5", "신발 > 운동화 > 조던 > 에어 조던 5", False),
    ("乔丹", "AJ6", "신발 > 운동화 > 조던 > 에어 조던 6", False),
    # --- 13 adidas专区 ---
    ("adidas专区", "adidas专区", "신발 > 운동화 > 아디다스", True),
    ("adidas专区", "YEEZY", "신발 > 운동화 > 아디다스 > 이지", False),
    ("adidas专区", "AD Originals Handball", "신발 > 운동화 > 아디다스 > 아디다스 핸드볼", False),
    ("adidas专区", "AD Wmns SL72 OG", "신발 > 운동화 > 아디다스 > SL72OG", False),
    ("adidas专区", "Pharrell x AD Adistar Jellyfish", "신발 > 운동화 > 아디다스 > 젤리피숴", False),
    ("adidas专区", "adizero", "신발 > 운동화 > 아디다스 > 아디제로", False),
    # --- 14 NIKE 专区 ---
    ("NIKE 专区", "NIKE 专区", "신발 > 운동화 > 나이키", True),
    ("NIKE 专区", "nike air max 95", "신발 > 운동화 > 나이키 > 나이키 에어맥스 95", False),
    ("NIKE 专区", "nike air max 97", "신발 > 운동화 > 나이키 > 나이키 에어맥스 97", False),
    ("NIKE 专区", "NIKE AIR ZOOM", "신발 > 운동화 > 나이키 > 나이키 에어줌", False),
    ("NIKE 专区", "nike sacai", "신발 > 운동화 > 나이키 > 나이키 사카이", False),
    ("NIKE 专区", "nike air force 1", "신발 > 운동화 > 나이키 > 나이키 에어포스 1", False),
    ("NIKE 专区", "Travis Scott x Air Jordan1", "신발 > 운동화 > 나이키 > 트래비스 스캇 x 에어 조던 1", False),
    ("NIKE 专区", "nike dunk low", "신발 > 운동화 > 나이키 > 나이키 덩크 로우", False),
    ("NIKE 专区", "nike Pegasus Premium", "신발 > 운동화 > 나이키 > 나이키 페가수스 프리미엄", False),
    ("NIKE 专区", "NIKE AIR MAX TN", "신발 > 운동화 > 나이키 > 나이키 에어맥스 TN", False),
    # --- 15 围巾 ---
    ("围巾", "围巾", "잡화 > 스카프", True),
    ("围巾", "迪奥 围巾", "잡화 > 스카프 > 디올", False),
    ("围巾", "古奇 围巾", "잡화 > 스카프 > 구찌", False),
    ("围巾", "巴宝莉 围巾", "잡화 > 스카프 > 버버리", False),
    # --- 16 리본 ---
    ("리본", "리본", "잡화 > 리본", True),
    ("리본", "디올", "잡화 > 리본 > 디올", False),
    ("리본", "HERMES", "잡화 > 리본 > 에르메스", False),
    # --- 17 선글라스 ---
    ("선글라스专区（男女款）", "선글라스专区（男女款）", "악세서리 > 선글라스 (남녀 공용)", True),
    ("선글라스专区（男女款）", "JACQUES MARIE MAGE", "악세서리 > 선글라스 (남녀 공용) > 자크 마리 마주", False),
    ("선글라스专区（男女款）", "TOM FORD", "악세서리 > 선글라스 (남녀 공용) > 톰 포드", False),
    ("선글라스专区（男女款）", "젠틀 몬스터", "악세서리 > 선글라스 (남녀 공용) > 젠틀 몬스터", False),
    ("선글라스专区（男女款）", "디올 선글라스", "악세서리 > 선글라스 (남녀 공용) > 디올", False),
    # --- 18 女装专区 ---
    ("女装专区", "女装专区", "의류 > 여성의류", True),
    ("女装专区", "Swimsuit", "의류 > 여성의류 > 수영복", False),
    ("女装专区", "GUCCI（女装）", "의류 > 여성의류 > 구찌", False),
    ("女装专区", "路易威登（女款）", "의류 > 여성의류 > 루이비통", False),
    ("女装专区", "prada（女款羽绒服）", "의류 > 여성의류 > 프라다", False),
    ("女装专区", "罗意威（女装）", "의류 > 여성의류 > 로에베", False),
    ("女装专区", "香奈儿（女装）", "의류 > 여성의류 > 샤넬", False),
    ("女装专区", "ALai (女装)", "의류 > 여성의류 > ALai", False),
    ("女装专区", "미우미우(여성옷)缪缪女装", "의류 > 여성의류 > 미우미우", False),
    # --- 19 여성옷 ---
    ("여성옷", "여성옷", "의류 > 여성의류", True),
    ("여성옷", "罗意威（女装）", "의류 > 여성의류 > 로에베", False),
    ("여성옷", "香奈儿（女装）", "의류 > 여성의류 > 샤넬", False),
    ("여성옷", "GUCCI", "의류 > 여성의류 > 구찌", False),
    ("여성옷", "路易威登（女款）", "의류 > 여성의류 > 루이비통", False),
    ("여성옷", "몽클레어", "의류 > 여성의류 > 몽클레어", False),
    ("여성옷", "ALai (女装)", "의류 > 여성의류 > ALai", False),
    ("여성옷", "미우미우(여성옷)缪缪女装", "의류 > 여성의류 > 미우미우", False),
]


def _split_path(s: str) -> list[str]:
    return [p.strip() for p in s.split(">") if p.strip()]


def _parse_meta_row(row: list) -> dict:
    if len(row) < 4:
        return {}
    m = row[3]
    return m if isinstance(m, dict) else {}


def build_json_rows(*, preserve_meta_path: Path | None = None) -> list[list]:
    preserved_ca: dict[tuple[str, str], str] = {}
    path = preserve_meta_path or Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")
    if path.is_file():
        raw_prev = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw_prev, list):
            for row in raw_prev:
                if not isinstance(row, list) or len(row) < 3:
                    continue
                meta = _parse_meta_row(row)
                v = meta.get("seven17_ca_id")
                if v is not None and str(v).strip():
                    preserved_ca[(str(row[0]), str(row[1]))] = str(v).strip()

    out: list[list] = []
    seen: set[tuple[str, str]] = set()
    for tup in RAW_ROWS:
        group, tag, path_str, anchor = tup[0], tup[1], tup[2], tup[3]
        tag_id = tup[4] if len(tup) > 4 else None
        key = (group, tag)
        if key in seen:
            raise ValueError(f"duplicate mapping for ({group!r}, {tag!r})")
        seen.add(key)
        row: list = [group, tag, _split_path(path_str)]
        meta: dict = {}
        if anchor:
            meta["anchor_only"] = True
        if tag_id is not None:
            meta["tag_id"] = int(tag_id)
        ca_prev = preserved_ca.get((group, tag))
        if ca_prev is not None:
            meta["seven17_ca_id"] = ca_prev
        if meta:
            row.append(meta)
        out.append(row)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="从 RAW_ROWS 生成 wecatalog_tag_category_map.json")
    ap.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="额外写入日志文件（UTF-8）",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="DEBUG")
    args = ap.parse_args()
    configure_module_logging(__name__, log_file=args.log_file, verbose=args.verbose)

    target = Path(__file__).resolve().with_name("wecatalog_tag_category_map.json")
    rows = build_json_rows()
    target.write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    _log.info(
        "%s",
        pf_kv(
            [("event", "mapbuild.wrote"), ("path", str(target)), ("rows", len(rows))],
            zh="已从 RAW 生成并写入分类映射表",
        ),
    )


if __name__ == "__main__":
    main()
