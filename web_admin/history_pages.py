"""Bounded history pages. Never download a whole history to sort it in Python."""
from datetime import datetime, timezone

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ORDER_FIELDS = '''order_id user_id username created_at product_name quantity status payment_method
amount_inr amount_usdt refund_status delivery_revoked api_purchase api_error_code api_error_message
replacement_report_id original_order_id original_order_ids manual_replacement_delivery
is_replacement admin_stock_delivery'''.split()


def _has_legacy_dates(collection):
    # Check each request: another service can insert a numeric/legacy timestamp
    # immediately after a previously all-date collection was inspected.
    return bool(collection.find_one({'$or': [
        {'created_at': {'$type': 'string'}}, {'created_at': {'$type': 'number'}}]}, {'_id': 1}))


def normalized_date_expr():
    field = '$created_at'
    kind = {'$type': field}
    return {'$switch': {'branches': [
        {'case': {'$eq': [kind, 'date']}, 'then': field},
        {'case': {'$in': [kind, ['int', 'long', 'double', 'decimal']]}, 'then': {
            '$toDate': {'$cond': [{'$gt': [field, 100000000000]}, field, {'$multiply': [field, 1000]}]}}},
        {'case': {'$eq': [kind, 'string']}, 'then': {'$dateFromString': {
            'dateString': {'$replaceAll': {'input': field, 'find': ' UTC', 'replacement': 'Z'}},
            'onError': _EPOCH, 'onNull': _EPOCH}}},
    ], 'default': _EPOCH}}


def page(collection, query=None, *, skip=0, limit=20, order_summary=False):
    """Filter/sort/skip/limit on MongoDB before returning rows to the application.

    Ordinary BSON dates use an indexable sort. Legacy timestamps are normalized
    on the server; only one requested page crosses the database connection.
    """
    skip = max(0, int(skip or 0))
    limit = max(0, min(500, int(limit or 0)))
    if not limit:
        return []
    if order_summary:
        projection = {**{key: 1 for key in ORDER_FIELDS}, 'has_items': {'$gt': [
            {'$cond': [{'$isArray': '$items'}, {'$size': '$items'}, 0]}, 0]}}
    else:
        projection = {'_history_sort_at': 0}
    if _has_legacy_dates(collection):
        # Sort only IDs/timestamps, then fetch at most the selected page. Large
        # delivered account arrays never enter the legacy in-memory sort.
        identities = list(collection.aggregate([
            {'$match': query or {}}, {'$project': {'created_at': 1}},
            {'$addFields': {'_history_sort_at': normalized_date_expr()}},
            {'$sort': {'_history_sort_at': -1, '_id': -1}}, {'$skip': skip}, {'$limit': limit},
            {'$project': {'_id': 1}},
        ], batchSize=limit + 1, comment='history_page:' + collection.name + ':legacy_ids'))
        ids = [row['_id'] for row in identities]
        if not ids:
            return []
        rows = list(collection.aggregate([{'$match': {'$and': [query or {}, {'_id': {'$in': ids}}]}}, {'$project': projection}],
            batchSize=limit + 1, comment='history_page:' + collection.name))
        by_id = {row['_id']: row for row in rows}
        return [by_id[identifier] for identifier in ids if identifier in by_id]
    return list(collection.aggregate([
        {'$match': query or {}}, {'$sort': {'created_at': -1, '_id': -1}},
        {'$skip': skip}, {'$limit': limit}, {'$project': projection},
    ], batchSize=limit + 1, comment='history_page:' + collection.name))
