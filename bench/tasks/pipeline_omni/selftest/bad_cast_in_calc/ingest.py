class DataIngester:
    """Loads raw order rows and keeps only the valid ones."""

    def __init__(self, rows):
        self.rows = rows

    def clean(self):
        good = []
        for row in self.rows:
            if row.get("item") and row.get("qty", 0) > 0:
                good.append({"item": row["item"], "price": row["price"], "qty": row["qty"]})
        return good
