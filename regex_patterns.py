import exrex

regex_patterns = [
    r"^^[A-Z]+${2}9W[123456LW]^[A-Z]+${10}$",
    r"^^[A-Z]+${2}(9A[45]|9C[45]|9E[45]|9F3|9G[2345]|9L[34]|9M[123456]|9Q[123]|9R4)^[A-Z]+${10}$",
    r"^^[A-Z]+${2}(30|L[23])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(8[12356789ABCDEGJKLNPQrXYZ]|6[123456])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}7[123456789ABCDEGJKLNPQrVWXYZ]^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(F[34567]|H[34AD]|K[1Z]|P[12345])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}([MWXYZ]C|[135678]H|[3BCDFKNP]J|[123]K|[3H]P)^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(M[12345]|Q[123])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(A[123ACDEGHKL])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(C[123])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}(L[01EH])^[A-Z]+${11}$",
    r"^^[A-Z]+${2}9K^[A-Z]+${11}$",
    r"^^[A-Z]+${2}9D^[A-Z]+${11}$",
    r"^\d{2}(0[1-9]|1[0-2])\d{5,}$",
    r"^\d{4}(0[1-9]|1[0-2])\d{3,}$",
    r"^R^[A-Z]+${6}J^[A-Z]+${4}$",
    r"^(EUA28^[A-Z]+${10})|(^[A-Z]+${2}9MICRO^[A-Z]+${7})$",
    r"^^[A-Z]+${2}HPB^[A-Z]+${10}$",
    r"^^[A-Z]+${2}(V[123456789]|[12345689ACDEFHJKLMNO]V)^[A-Z]+${11}$",
]


def generate_strings_from_regexes(regex_list):
    """
    根据正则表达式列表生成对应的随机字符串列表

    参数:
        regex_list: 包含正则表达式的列表

    返回:
        list: 包含每个正则表达式所生成字符串的列表
    """
    generated_strings = []
    for pattern in regex_list:
        try:
            # 使用 exrex 生成符合当前正则表达式的随机字符串
            generated_string = exrex.getone(pattern)
            generated_strings.append(generated_string)
        except Exception as e:
            print(f"无法为模式 '{pattern}' 生成字符串: {e}")
            generated_strings.append(None)  # 如果生成失败，添加 None 作为占位符
    return generated_strings


# 生成字符串
result_strings = generate_strings_from_regexes(regex_patterns)

# 打印结果
print("生成的字符串数组:")
print(result_strings)

# 如果需要更详细的输出，可以遍历打印
print("\n详细信息:")
for i, (pattern, string) in enumerate(zip(regex_patterns, result_strings)):
    print(f"模式 {i + 1}: '{pattern}' -> 生成: '{string}'")