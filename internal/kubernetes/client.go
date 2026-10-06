package kubernetes

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"strings"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
)

type Client struct {
	clientset *kubernetes.Clientset
}

func NewClient() (*Client, error) {
	// Use in-cluster config when running inside Kubernetes
	config, err := rest.InClusterConfig()
	if err != nil {
		// Fallback to out-of-cluster or just return error in a strict setup
		return nil, fmt.Errorf("failed to load in-cluster config: %v", err)
	}

	clientset, err := kubernetes.NewForConfig(config)
	if err != nil {
		return nil, fmt.Errorf("failed to create kubernetes clientset: %v", err)
	}

	return &Client{
		clientset: clientset,
	}, nil
}

func (c *Client) GetDeploymentStatus(ctx context.Context, namespace, deploymentName string) (string, error) {
	deploy, err := c.clientset.AppsV1().Deployments(namespace).Get(ctx, deploymentName, metav1.GetOptions{})
	if err != nil {
		return "", fmt.Errorf("failed to get deployment %s in namespace %s: %v", deploymentName, namespace, err)
	}

	status := fmt.Sprintf("Deployment: %s\nNamespace: %s\nReplicas: %d / %d (Ready: %d, Available: %d)\n",
		deploy.Name, deploy.Namespace,
		deploy.Status.Replicas, *deploy.Spec.Replicas,
		deploy.Status.ReadyReplicas, deploy.Status.AvailableReplicas)

	// Get recent events for this deployment
	events, err := c.clientset.CoreV1().Events(namespace).List(ctx, metav1.ListOptions{
		FieldSelector: fmt.Sprintf("involvedObject.name=%s,involvedObject.kind=Deployment", deploymentName),
	})

	if err == nil && len(events.Items) > 0 {
		status += "\nRecent Events:\n"
		for _, e := range events.Items {
			status += fmt.Sprintf("- [%s] %s: %s\n", e.Type, e.Reason, e.Message)
		}
	}

	return status, nil
}

func (c *Client) SearchPodLogs(ctx context.Context, namespace, podPrefix string, maxLines int64) (string, error) {
	// Cap the max lines to prevent massive dumps
	if maxLines <= 0 || maxLines > 500 {
		maxLines = 500
	}

	// Find the pod by prefix
	pods, err := c.clientset.CoreV1().Pods(namespace).List(ctx, metav1.ListOptions{})
	if err != nil {
		return "", fmt.Errorf("failed to list pods in namespace %s: %v", namespace, err)
	}

	var targetPod *corev1.Pod
	for _, p := range pods.Items {
		if strings.HasPrefix(p.Name, podPrefix) {
			targetPod = &p
			break
		}
	}

	if targetPod == nil {
		return "", fmt.Errorf("no pod found matching prefix '%s'", podPrefix)
	}

	req := c.clientset.CoreV1().Pods(namespace).GetLogs(targetPod.Name, &corev1.PodLogOptions{
		TailLines: &maxLines,
	})

	podLogs, err := req.Stream(ctx)
	if err != nil {
		return "", fmt.Errorf("error in opening stream: %v", err)
	}
	defer podLogs.Close()

	buf := new(bytes.Buffer)
	// Read with a hard cap on bytes as an additional safety measure (e.g. max 5MB)
	_, err = io.Copy(buf, io.LimitReader(podLogs, 5*1024*1024))
	if err != nil {
		return "", fmt.Errorf("error reading stream: %v", err)
	}

	return buf.String(), nil
}
