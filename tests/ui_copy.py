"""Keep semantic copy checks independent of optional inline glossary expansions."""
def without_glossary(text):
    for expanded, short in {
        "OI（未平倉量）": "OI", "Entry（進場）": "Entry",
        "SL（止損）": "SL", "TP（止盈）": "TP",
        "API（應用程式介面）": "API", "CVD（累積成交量差）": "CVD",
    }.items():
        text = text.replace(expanded, short)
    return text
