package main

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

func TestServiceAccountTokenRotation(t *testing.T) {
	path := filepath.Join(t.TempDir(), "token")
	want := "old-token"
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer "+want {
			t.Error("did not reload projected token")
			w.WriteHeader(401)
			return
		}
		w.WriteHeader(204)
	}))
	defer server.Close()
	api := API{client: server.Client(), base: server.URL, tokenPath: path}
	for _, token := range []string{"old-token", "rotated-token"} {
		want = token
		if err := os.WriteFile(path, []byte(token), 0600); err != nil {
			t.Fatal(err)
		}
		if err := api.request(context.Background(), "GET", "/", nil, nil); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := api.request(context.Background(), "GET", "/", nil, nil); err == nil {
		t.Fatal("used stale credentials after token disappeared")
	}
}

func TestConflictRereadsOwnership(t *testing.T) {
	// Another node wins the CAS between our read and write. A retry must inspect
	// that winner rather than applying the stale ownership decision again.
	v := Volume{ID: "v-0123456789abcdef0123456789abcdef"}
	writes := 0
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			b, _ := json.Marshal(v)
			cm := configMap{Data: map[string]string{"volume.json": string(b)}}
			cm.Metadata.ResourceVersion = "1"
			json.NewEncoder(w).Encode(cm)
			return
		}
		writes++
		v.Owner = "s2"
		v.Epoch = 1
		w.WriteHeader(http.StatusConflict)
	}))
	defer server.Close()
	api := API{client: server.Client(), base: server.URL, namespace: "test"}
	_, err := api.Edit(context.Background(), v.ID, func(v *Volume) error { return acquire(v, "s1") })
	if err == nil || writes != 1 {
		t.Fatalf("ownership conflict was ignored: writes=%d err=%v", writes, err)
	}
}
