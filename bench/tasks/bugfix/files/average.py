def average(numbers):
    total = 0
    for n in numbers:
        total += n
    return total / len(numbers)


def median(numbers):
    s = numbers
    s.sort()
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return s[mid]
    return s[mid]


if __name__ == "__main__":
    print(average([1, 2, 3, 4]))
    print(median([1, 2, 3, 4]))
    print(average([]))
