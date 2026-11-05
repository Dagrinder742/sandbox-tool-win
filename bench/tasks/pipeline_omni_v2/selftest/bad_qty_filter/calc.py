TAX_RATE = 0.08


def subtotal(rows):
    total = 0.0
    for row in rows:
        total += row["price"] * row["qty"]
    return total


def average_order(rows):
    if not rows:
        return 0.0
    return subtotal(rows) / len(rows)


def summary(rows):
    sub = subtotal(rows)
    tax = round(sub * TAX_RATE, 2)
    return {"orders": len(rows), "subtotal": round(sub, 2), "tax": tax,
            "total": round(sub + tax, 2), "average": round(average_order(rows), 2)}
