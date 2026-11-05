import csv

total = 0.0
with open("sales.csv", newline="") as f:
    rows = list(csv.reader(f))
for row in rows[2:]:
    total += int(row[1]) * float(row[2])
print("Total: %.2f" % total)
