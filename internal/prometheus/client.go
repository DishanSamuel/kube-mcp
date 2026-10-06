package prometheus

import (
	"context"
	"fmt"
	"os"
	"time"

	"github.com/prometheus/client_golang/api"
	v1 "github.com/prometheus/client_golang/api/prometheus/v1"
	"github.com/prometheus/common/model"
)

type Client struct {
	api v1.API
}

func NewClient() (*Client, error) {
	// The Prometheus address is typically injected via environment variable in a Kubernetes sidecar setup
	addr := os.Getenv("PROMETHEUS_URL")
	if addr == "" {
		addr = "http://prometheus-operated.monitoring.svc.cluster.local:9090" // Default internal K8s address
	}

	client, err := api.NewClient(api.Config{
		Address: addr,
	})
	if err != nil {
		return nil, fmt.Errorf("error creating prometheus client: %v", err)
	}

	v1api := v1.NewAPI(client)

	return &Client{
		api: v1api,
	}, nil
}

func (c *Client) GetProcessMetrics(ctx context.Context, target, metricType string) (string, error) {
	var query string

	// Safety: Translate abstract intents into strictly bounded, hardcoded PromQL queries.
	// This prevents the LLM from injecting arbitrary PromQL that could crash Prometheus.
	switch metricType {
	case "cpu":
		query = fmt.Sprintf(`rate(container_cpu_usage_seconds_total{pod=~"%s.*"}[5m])`, target)
	case "memory":
		query = fmt.Sprintf(`container_memory_working_set_bytes{pod=~"%s.*"}`, target)
	default:
		return "", fmt.Errorf("unsupported metric_type: %s (supported: cpu, memory)", metricType)
	}

	result, warnings, err := c.api.Query(ctx, query, time.Now())
	if err != nil {
		return "", fmt.Errorf("error querying prometheus: %v", err)
	}
	if len(warnings) > 0 {
		fmt.Printf("Prometheus Warnings: %v\n", warnings)
	}

	// Format the output concisely for the LLM
	var output string
	switch result.Type() {
	case model.ValVector:
		vector := result.(model.Vector)
		if len(vector) == 0 {
			return fmt.Sprintf("No data found for target: %s", target), nil
		}
		
		// Cap the output to prevent massive payloads
		maxItems := 10
		for i, sample := range vector {
			if i >= maxItems {
				output += fmt.Sprintf("... (truncated %d more time series)\n", len(vector)-maxItems)
				break
			}
			pod := sample.Metric["pod"]
			output += fmt.Sprintf("Pod: %s => Value: %s\n", pod, sample.Value.String())
		}
	default:
		return "", fmt.Errorf("unexpected prometheus result type: %v", result.Type())
	}

	return output, nil
}
