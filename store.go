package main

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"time"
)

type Volume struct {
	ID               string          `json:"id"`
	Database         string          `json:"database"`
	Capacity         int64           `json:"capacity"`
	Owner            string          `json:"owner,omitempty"`
	Epoch            int64           `json:"epoch"`
	Prepared         bool            `json:"prepared"`
	Clean            bool            `json:"clean"`
	Initialized      bool            `json:"initialized"`
	Deleted          bool            `json:"deleted"`
	RecoveryPolicy   string          `json:"recoveryPolicy,omitempty"`
	ReplicaDirectory string          `json:"replicaDirectory,omitempty"`
	RecoverySeed     string          `json:"recoverySeed,omitempty"`
	SeedDigest       string          `json:"seedDigest,omitempty"`
	SeedClaimedEpoch int64           `json:"seedClaimedEpoch,omitempty"`
	Recovery         *RecoveryRecord `json:"recovery,omitempty"`
}
type configMap struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Metadata   struct {
		Name            string            `json:"name"`
		ResourceVersion string            `json:"resourceVersion,omitempty"`
		Labels          map[string]string `json:"labels,omitempty"`
	} `json:"metadata"`
	Data map[string]string `json:"data"`
}
type Store interface {
	Get(context.Context, string) (Volume, error)
	Create(context.Context, Volume) (Volume, error)
	Edit(context.Context, string, func(*Volume) error) (Volume, error)
}
type API struct {
	client                 *http.Client
	base, token, namespace string
	tokenPath              string
}
type apiError struct {
	code int
	body string
}

func (e *apiError) Error() string      { return fmt.Sprintf("Kubernetes API HTTP %d", e.code) }
func apiCode(err error, code int) bool { e, ok := err.(*apiError); return ok && e.code == code }
func newAPI(namespace string) (*API, error) {
	token, e := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/token")
	if e != nil {
		return nil, e
	}
	ca, e := os.ReadFile("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
	if e != nil {
		return nil, e
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("invalid service account CA")
	}
	return &API{client: &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{TLSClientConfig: &tls.Config{RootCAs: roots, MinVersion: tls.VersionTLS12}}}, base: "https://" + os.Getenv("KUBERNETES_SERVICE_HOST") + ":" + os.Getenv("KUBERNETES_SERVICE_PORT"), token: string(token), tokenPath: "/var/run/secrets/kubernetes.io/serviceaccount/token", namespace: namespace}, nil
}
func (a *API) request(ctx context.Context, method, path string, body any, out any) error {
	var input bytes.Buffer
	if body != nil {
		if e := json.NewEncoder(&input).Encode(body); e != nil {
			return e
		}
	}
	r, e := http.NewRequestWithContext(ctx, method, a.base+path, &input)
	if e != nil {
		return e
	}
	token := a.token
	if a.tokenPath != "" {
		b, err := os.ReadFile(a.tokenPath)
		if err != nil {
			return err
		}
		token = string(b)
	}
	r.Header.Set("Authorization", "Bearer "+token)
	r.Header.Set("Content-Type", "application/json")
	if method == "PATCH" {
		r.Header.Set("Content-Type", "application/json-patch+json")
	}
	response, e := a.client.Do(r)
	if e != nil {
		return e
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return &apiError{response.StatusCode, string(b)}
	}
	if out != nil {
		return json.NewDecoder(response.Body).Decode(out)
	}
	return nil
}
func (a *API) path(id string) string {
	p := "/api/v1/namespaces/" + a.namespace + "/configmaps"
	if id != "" {
		p += "/" + id
	}
	return p
}
func (a *API) getCM(ctx context.Context, id string) (configMap, Volume, error) {
	var cm configMap
	var v Volume
	e := a.request(ctx, "GET", a.path(id), nil, &cm)
	if e != nil {
		return cm, v, e
	}
	e = json.Unmarshal([]byte(cm.Data["volume.json"]), &v)
	return cm, v, e
}
func (a *API) Get(ctx context.Context, id string) (Volume, error) {
	_, v, e := a.getCM(ctx, id)
	return v, e
}
func (a *API) Create(ctx context.Context, v Volume) (Volume, error) {
	cm := configMap{APIVersion: "v1", Kind: "ConfigMap", Data: map[string]string{}}
	cm.Metadata.Name = v.ID
	cm.Metadata.Labels = map[string]string{"sqlite.csi/volume": "true"}
	b, _ := json.Marshal(v)
	cm.Data["volume.json"] = string(b)
	e := a.request(ctx, "POST", a.path(""), cm, nil)
	if apiCode(e, 409) {
		return a.Get(ctx, v.ID)
	}
	return v, e
}
func (a *API) Edit(ctx context.Context, id string, change func(*Volume) error) (Volume, error) {
	for i := 0; i < 12; i++ {
		cm, v, e := a.getCM(ctx, id)
		if e != nil {
			return v, e
		}
		if e = change(&v); e != nil {
			return v, e
		}
		b, _ := json.Marshal(v)
		cm.Data["volume.json"] = string(b)
		e = a.request(ctx, "PUT", a.path(id), cm, nil)
		if apiCode(e, 409) {
			continue
		}
		return v, e
	}
	return Volume{}, fmt.Errorf("concurrent volume update; retry")
}
