package main

import "testing"

func TestSignatureMatchesPythonImplementation(t *testing.T) {
	actual := signatureValue(requestPath, "test-token", "en", "1723334400123", 42)
	const expected = "7931cd6d4d0445f586a027fd7c825e88.42"
	if actual != expected {
		t.Fatalf("signature mismatch: got %q, want %q", actual, expected)
	}
}

func TestRequestBodyIsFixed(t *testing.T) {
	const expected = `{"pageSize":1000,"currentPage":1,"total":165039,"condition":{"status":0,"plantName":"","deviceSN":"","odmSN":"","moduleSN":"","country":"","deviceType":"","productType":"","queryDate":{"begin":0,"end":0}}}`
	if requestBody != expected {
		t.Fatalf("request body changed: %s", requestBody)
	}
}
