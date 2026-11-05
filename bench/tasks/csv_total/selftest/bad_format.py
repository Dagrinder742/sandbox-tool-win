import csv

total = 0.0
with open("sales.csv", newline="") as f:
    for row in csv.DictReader(f):
        total += int(row["qty"]) * float(row["price"])
print("Total:", total)
