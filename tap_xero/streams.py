import time
import json
from requests.exceptions import HTTPError
import singer
from singer import metadata, metrics, Transformer
from singer.utils import strptime_with_tz
from . import transform

LOGGER = singer.get_logger()
FULL_PAGE_SIZE = 100


def _make_request(ctx, tap_stream_id, filter_options=None, attempts=0):
    filter_options = filter_options or {}
    try:
        return ctx.client.fetch(tap_stream_id, **filter_options)
    except HTTPError as e:
        if e.response.status_code == 401:
            if attempts >= 1:
                raise Exception(
                    "Received Not Authorized response after credential refresh."
                ) from e
            ctx.refresh_credentials()
            return _make_request(ctx, tap_stream_id, filter_options, attempts + 1)
        elif e.response.status_code == 429 or e.response.status_code == 503:
            if attempts >= 5:
                raise Exception(
                    "Still rate-limited after waiting for the retry period multiple times."
                ) from e
            wait = float(e.response.headers["Retry-After"])
            if wait > 60:
                raise Exception(
                    f"Wait of {wait}s is over 60s so have hit daily rate limit"
                ) from e
            LOGGER.info(f"Waiting for rate limit: {wait}")
            time.sleep(wait)
            return _make_request(ctx, tap_stream_id, filter_options, attempts + 1)
        else:
            raise
    assert False


def write_sub_records(
    ctx, parent_pk, sub, records, id_key=None, parent_key="LineItems"
):
    rows = [
        {
            **row,
            "ParentID": parent[parent_pk],
            # Xero thinks it's reasonable for some streams to not return a LineItemID, so create one if required using the parent's ID and the item's index
            # See https://www.notion.so/fosters/tap-xero-aaf6c7d5a008445f8a9efa0d956570d3#da301fa942024cf880e1859660a172d1
            "LineItemID": f"{parent[parent_pk]}|{row_index}"
            if id_key is None
            else row[id_key],
            # Have to JSON-encode so linebreaks aren't stripped out by Redshift loader
            "Description": json.dumps(row.get("Description")),
            "Tracking": row["Tracking"][0] if len(row["Tracking"]) > 0 else None,
        }
        for parent in records
        for (row_index, row) in enumerate(parent[parent_key])
    ]
    sub.write_records(rows, ctx)


class Stream:
    def __init__(
        self, tap_stream_id, pk_fields, bookmark_key="UpdatedDateUTC", format_fn=None
    ):
        self.tap_stream_id = tap_stream_id
        self.pk_fields = pk_fields
        self.format_fn = format_fn or (lambda x: x)
        self.bookmark_key = bookmark_key
        self.replication_method = "INCREMENTAL"

    def metrics(self, records):
        with metrics.record_counter(self.tap_stream_id) as counter:
            counter.increment(len(records))

    def write_records(self, records, ctx):
        stream = ctx.catalog.get_stream(self.tap_stream_id)
        schema = stream.schema.to_dict()
        mdata = stream.metadata
        for rec in records:
            with Transformer() as transformer:
                rec = transformer.transform(rec, schema, metadata.to_map(mdata))
                singer.write_record(self.tap_stream_id, rec)
        self.metrics(records)


class BookmarkedStream(Stream):
    def sync(self, ctx, sub=None):
        bookmark = [self.tap_stream_id, self.bookmark_key]
        start = ctx.get_bookmark(bookmark)
        records = _make_request(ctx, self.tap_stream_id, dict(since=start))
        if records:
            self.format_fn(records)
            self.write_records(records, ctx)
            if sub:
                write_sub_records(ctx, self.pk_fields[0], sub, records)
            max_bookmark_value = max([record[self.bookmark_key] for record in records])
            ctx.set_bookmark(bookmark, max_bookmark_value)
            ctx.write_state()


class PaginatedStream(Stream):
    def sync(self, ctx, sub=None):
        bookmark = [self.tap_stream_id, self.bookmark_key]
        offset = [self.tap_stream_id, "page"]
        start = ctx.get_bookmark(bookmark)
        curr_page_num = ctx.get_offset(offset) or 1
        filter_options = dict(since=start, order="UpdatedDateUTC ASC")
        max_updated = start
        while True:
            ctx.set_offset(offset, curr_page_num)
            ctx.write_state()
            filter_options["page"] = curr_page_num
            records = _make_request(ctx, self.tap_stream_id, filter_options)
            if records:
                self.format_fn(records)
                self.write_records(records, ctx)
                if sub:
                    write_sub_records(
                        ctx,
                        self.pk_fields[0],
                        sub,
                        records,
                        parent_key="JournalLines"
                        if self.tap_stream_id == "manual_journals"
                        else "LineItems",
                    )
                max_updated = records[-1][self.bookmark_key]
            if not records or len(records) < FULL_PAGE_SIZE:
                break
            curr_page_num += 1
        ctx.clear_offsets(self.tap_stream_id)
        ctx.set_bookmark(bookmark, max_updated)
        ctx.write_state()


class Journals(Stream):
    """The Journals endpoint is a special case. It has its own way of ordering
    and paging the data. See
    https://developer.xero.com/documentation/api/journals"""

    def sync(self, ctx, sub=None):
        bookmark = [self.tap_stream_id, self.bookmark_key]
        journal_number = ctx.get_bookmark(bookmark) or 0
        while True:
            filter_options = {"offset": journal_number}
            records = _make_request(ctx, self.tap_stream_id, filter_options)
            if records:
                self.format_fn(records)
                self.write_records(records, ctx)
                if sub:
                    rows = [
                        {
                            **row,
                            "JournalID": parent["JournalID"],
                            # Have to JSON-encode so linebreaks aren't stripped out by Redshift loader
                            "Description": json.dumps(row.get("Description")),
                            "Tracking": row["TrackingCategories"][0]
                            if len(row["TrackingCategories"]) > 0
                            else None,
                        }
                        for parent in records
                        for row in parent["JournalLines"]
                    ]
                    sub.write_records(rows, ctx)
                journal_number = max((record[self.bookmark_key] for record in records))
                ctx.set_bookmark(bookmark, journal_number)
                ctx.write_state()
            if not records or len(records) < FULL_PAGE_SIZE:
                break


class LinkedTransactions(Stream):
    """The Linked Transactions endpoint is a special case. It supports
    pagination, but not the Modified At header, but the objects returned have
    the UpdatedDateUTC timestamp in them. Therefore we must always iterate over
    all of the data, but we can manually omit records based on the
    UpdatedDateUTC property."""

    def sync(self, ctx, sub=None):
        bookmark = [self.tap_stream_id, self.bookmark_key]
        offset = [self.tap_stream_id, "page"]
        # start = ctx.update_start_date_bookmark(bookmark)
        start = ctx.get_bookmark(bookmark)
        curr_page_num = ctx.get_offset(offset) or 1
        max_updated = start
        while True:
            ctx.set_offset(offset, curr_page_num)
            ctx.write_state()
            filter_options = {"page": curr_page_num}
            raw_records = _make_request(ctx, self.tap_stream_id, filter_options)
            records = [
                x
                for x in raw_records
                if strptime_with_tz(x[self.bookmark_key]) >= strptime_with_tz(start)
            ]
            if records:
                self.write_records(records, ctx)
                max_updated = records[-1][self.bookmark_key]
            if not records or len(records) < FULL_PAGE_SIZE:
                break
            curr_page_num += 1
        ctx.clear_offsets(self.tap_stream_id)
        ctx.set_bookmark(bookmark, max_updated)
        ctx.write_state()


class Everything(Stream):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bookmark_key = None
        self.replication_method = "FULL_TABLE"

    def sync(self, ctx, sub=None):
        records = _make_request(ctx, self.tap_stream_id)
        transformed = self.format_fn(records)
        self.write_records(records if transformed is None else transformed, ctx)
        if sub:
            write_sub_records(ctx, self.pk_fields[0], sub, records)


class SubStream(Stream):
    def __init__(self, name, *args, **kwargs):
        super().__init__(name, ["LineItemID"], *args, **kwargs)


all_streams = [
    # PAGINATED STREAMS
    # These endpoints have all the best properties: they return the
    # UpdatedDateUTC property and support the Modified After, order, and page
    # parameters
    PaginatedStream("bank_transactions", ["BankTransactionID"]),
    PaginatedStream("contacts", ["ContactID"], format_fn=transform.format_contacts),
    PaginatedStream(
        "credit_notes", ["CreditNoteID"], format_fn=transform.format_credit_notes
    ),
    PaginatedStream("invoices", ["InvoiceID"], format_fn=transform.format_invoices),
    PaginatedStream("manual_journals", ["ManualJournalID"]),
    PaginatedStream("overpayments", ["OverpaymentID"]),
    PaginatedStream("prepayments", ["PrepaymentID"]),
    PaginatedStream("purchase_orders", ["PurchaseOrderID"]),
    # JOURNALS STREAM
    # This endpoint is paginated, but in its own special snowflake way.
    Journals(
        "journals",
        ["JournalID"],
        bookmark_key="JournalNumber",
        format_fn=transform.format_journals,
    ),
    # NON-PAGINATED STREAMS
    # These endpoints do not support pagination, but do support the Modified At
    # header.
    BookmarkedStream("accounts", ["AccountID"]),
    BookmarkedStream(
        "bank_transfers", ["BankTransferID"], bookmark_key="CreatedDateUTC"
    ),
    BookmarkedStream("employees", ["EmployeeID"]),
    BookmarkedStream("expense_claims", ["ExpenseClaimID"]),
    BookmarkedStream("items", ["ItemID"]),
    BookmarkedStream("payments", ["PaymentID"]),
    BookmarkedStream("receipts", ["ReceiptID"], format_fn=transform.format_receipts),
    BookmarkedStream("users", ["UserID"], format_fn=transform.format_users),
    # PULL EVERYTHING STREAMS
    # These endpoints do not support the Modified After header (or paging), so
    # we must pull all the data each time.
    Everything(
        "contact_groups", ["ContactGroupID"], format_fn=transform.format_contact_groups
    ),
    Everything("currencies", ["Code"]),
    Everything("organisations", ["OrganisationID"]),
    Everything("repeating_invoices", ["RepeatingInvoiceID"]),
    Everything("tax_rates", ["TaxType"]),
    Everything(
        "tracking_categories",
        ["TrackingOptionID"],
        format_fn=transform.format_tracking_categories,
    ),
    # LINKED TRANSACTIONS STREAM
    # This endpoint is not paginated, but can do some manual filtering
    LinkedTransactions(
        "linked_transactions", ["LinkedTransactionID"], bookmark_key="UpdatedDateUTC"
    ),
    # SUBSTREAMS
    # These aren't specific endpoints, they're substreams on existing endpoints
    # Splitting into separate substreams because pipelinewise-target-redshift doesn't create new tables for them automatically
    SubStream("bank_transactions_lines"),
    SubStream("credit_notes_lines"),
    SubStream("invoices_lines"),
    SubStream("journals_lines"),
    SubStream("manual_journals_lines"),
    SubStream("overpayments_lines"),
    SubStream("prepayments_lines"),
    SubStream("purchase_orders_lines"),
    SubStream("receipts_lines"),
    SubStream("repeating_invoices_lines"),
]
all_stream_ids = [s.tap_stream_id for s in all_streams]

sub_stream_suffix = "_lines"
sub_stream_ids = {s for s in all_stream_ids if s.endswith(sub_stream_suffix)}
has_sub_stream_ids = {
    s for s in all_stream_ids if (s + sub_stream_suffix) in sub_stream_ids
}
