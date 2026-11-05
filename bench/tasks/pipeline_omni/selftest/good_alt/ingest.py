class DataIngester:
    """Loads raw order rows and keeps only the valid ones."""

    def __init__(self, rows):
        self.rows = rows

    def clean(self):
        return [{"item": r["item"], "price": float(r["price"]), "qty": r["qty"]}
                for r in self.rows if isinstance(r.get("item"), str) and r["item"] and r.get("qty", 0) > 0]
