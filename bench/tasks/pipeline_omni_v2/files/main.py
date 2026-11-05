from ingest import DataIngester
from calc import summary

RAW = [
    {"item": "pen", "price": "1.50", "qty": 4},
    {"item": "book", "price": "12.00", "qty": 1},
    {"item": "", "price": "3.00", "qty": 2},
    {"item": "mug", "price": "8.25", "qty": 0},
    {"item": "lamp", "price": "20.00", "qty": 2},
]


def report(rows):
    s = summary(rows)
    print(f"orders={s['orders']} subtotal={s['subtotal']:.2f} tax={s['tax']:.2f} "
          f"total={s['total']:.2f} average={s['average']:.2f}")


if __name__ == "__main__":
    report(DataIngester(RAW).clean())
    report(DataIngester([]).clean())
