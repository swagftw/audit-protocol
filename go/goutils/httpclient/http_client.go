package httpclient

import (
	"net/http"
	"time"

	log "github.com/sirupsen/logrus"
	"golang.org/x/time/rate"

	"audit-protocol/goutils/settings"
)

func GetIPFSHTTPClient(settingsObj *settings.SettingsObj) http.Client {
	transport := http.Transport{
		MaxIdleConns:        settingsObj.Web3Storage.MaxIdleConns,
		MaxConnsPerHost:     settingsObj.Web3Storage.MaxIdleConns,
		MaxIdleConnsPerHost: settingsObj.Web3Storage.MaxIdleConns,
		IdleConnTimeout:     time.Duration(settingsObj.Web3Storage.IdleConnTimeout),
		DisableCompression:  true,
	}

	ipfsHTTPClient := http.Client{
		Timeout:   time.Duration(settingsObj.PruningServiceSettings.IpfsTimeout) * time.Second,
		Transport: &transport,
	}

	return ipfsHTTPClient
}

func GetW3sClient(settingsObj *settings.SettingsObj) (http.Client, *rate.Limiter) {
	t := http.Transport{
		//TLSClientConfig:    &tls.Config{KeyLogWriter: kl, InsecureSkipVerify: true},
		MaxIdleConns:        settingsObj.Web3Storage.MaxIdleConns,
		MaxConnsPerHost:     settingsObj.Web3Storage.MaxIdleConns,
		MaxIdleConnsPerHost: settingsObj.Web3Storage.MaxIdleConns,
		IdleConnTimeout:     time.Duration(settingsObj.Web3Storage.IdleConnTimeout),
		DisableCompression:  true,
	}

	w3sHttpClient := http.Client{
		Timeout:   time.Duration(settingsObj.PruningServiceSettings.Web3Storage.TimeoutSecs) * time.Second,
		Transport: &t,
	}

	//Default values
	tps := rate.Limit(1) //3 TPS
	burst := 1
	if settingsObj.PruningServiceSettings.Web3Storage.RateLimit != nil {
		burst = settingsObj.PruningServiceSettings.Web3Storage.RateLimit.Burst
		if settingsObj.PruningServiceSettings.Web3Storage.RateLimit.RequestsPerSec == -1 {
			tps = rate.Inf
			burst = 0
		} else {
			tps = rate.Limit(settingsObj.PruningServiceSettings.Web3Storage.RateLimit.RequestsPerSec)
		}
	}
	log.Infof("Rate Limit configured for web3.storage at %v TPS with a burst of %d", tps, burst)
	web3StorageClientRateLimiter := rate.NewLimiter(tps, burst)

	return w3sHttpClient, web3StorageClientRateLimiter
}