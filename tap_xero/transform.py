def format_credit_notes(credit_notes):
    for credit_note in credit_notes:
        credit_note.pop("Payments", None)


def format_contact_groups(contact_groups):
    for contact_group in contact_groups:
        contact_group.pop("Contacts", None)


def strip_warnings(records):
    for record in records:
        record.pop("Warnings", None)


format_users = strip_warnings


def format_receipts(receipts):
    strip_warnings(receipts)
    for receipt in receipts:
        receipt.get("User", {}).pop("Warnings", None)
        receipt.get("Contact", {}).pop("Warnings", None)


def format_contacts(contacts):
    strip_warnings(contacts)
    for contact in contacts:
        format_contact_groups(contact["ContactGroups"])


def format_invoices(invoices):
    # NB: Xero sometimes formats the Date as '/Date(0+0000)/' to indicate
    # it is 0 milliseconds from the unix epoch. Convert this to a datetime
    # that will be accepted by the transformer. This should not cause
    # inconsitencies because the 'Date' is normally returned as an iso8601
    # string and this edge case causes it to be returned differently
    for invoice in invoices:
        if invoice.get("Date") == "/Date(0+0000)/":
            invoice["Date"] = "1970-01-01T00:00:00.000000Z"


def format_journals(journals):
    # NB: Xero sometimes formats the JournalDate as '/Date(0+0000)/' to
    # indicate it is 0 milliseconds from the unix epoch. Convert this to a
    # datetime that will be accepted by the transformer. This should not
    # cause inconsitencies because the 'Date' is normally returned as an
    # iso8601 string and this edge case causes it to be returned
    # differently
    for journal in journals:
        if journal.get("JournalDate") == "/Date(0+0000)/":
            journal["JournalDate"] = "1970-01-01T00:00:00.000000Z"
