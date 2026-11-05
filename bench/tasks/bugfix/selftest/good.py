def average(numbers):
    if not numbers:
        return 0.0
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def median(numbers):
    s = sorted(numbers)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]


if __name__ == "__main__":
    print(average([1, 2, 3, 4]))
    print(median([1, 2, 3, 4]))
    print(average([]))
