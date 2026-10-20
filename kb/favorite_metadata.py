"""收藏关联查询共用的元数据列与 LEFT JOIN,别名固定为 f、d。"""

FAVORITE_DOCUMENT_COLUMNS = (
    " (d.path IS NOT NULL) AS document_available,"
    " COALESCE(d.source, NULLIF(f.origin_source, '')) AS source,"
    " COALESCE(NULLIF(d.title, ''), f.origin_title) AS title,"
    " COALESCE(d.account, f.origin_account) AS account,"
    " COALESCE(d.published, f.origin_published) AS published,"
    " COALESCE(NULLIF(d.url, ''), f.origin_url) AS url,"
    " COALESCE(d.summary, f.origin_summary) AS summary"
)

FAVORITE_DOCUMENT_JOIN = " FROM favorites f LEFT JOIN documents d ON d.path = f.path"
