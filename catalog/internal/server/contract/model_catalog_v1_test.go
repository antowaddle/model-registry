package contract_test

import (
	"encoding/json"
	"io"
	"mime"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strconv"
	"testing"
	"time"

	"github.com/kubeflow/hub/catalog/internal/catalog/basecatalog"
	"github.com/kubeflow/hub/catalog/internal/catalog/modelcatalog"
	"github.com/kubeflow/hub/catalog/internal/catalog/modelcatalog/models"
	modelservice "github.com/kubeflow/hub/catalog/internal/catalog/modelcatalog/service"
	sharedmodels "github.com/kubeflow/hub/catalog/internal/db/models"
	"github.com/kubeflow/hub/catalog/internal/db/service"
	"github.com/kubeflow/hub/catalog/internal/server/openapi/v1"
	"github.com/kubeflow/hub/catalog/internal/testhelpers"
	model "github.com/kubeflow/hub/catalog/pkg/openapi"
	dbmodels "github.com/kubeflow/hub/internal/platform/db/entity"
	"github.com/kubeflow/hub/internal/testutils"
	"github.com/stretchr/testify/require"
)

const v1Root = "/api/model_catalog/v1"

func TestMain(m *testing.M) {
	os.Exit(testutils.TestMainPostgresHelper(m))
}

// These hand-written projections are the consumer contract, not generated API
// models. Pointers distinguish missing/null fields from valid zero values.
// Unknown fields are deliberately accepted: additive changes are compatible.
type consumerPage[T any] struct {
	Items         []T     `json:"items"`
	NextPageToken *string `json:"nextPageToken"`
	PageSize      *int    `json:"pageSize"`
	Size          *int    `json:"size"`
}

type consumerSource struct {
	ID      *string  `json:"id"`
	Name    *string  `json:"name"`
	Labels  []string `json:"labels"`
	Enabled *bool    `json:"enabled"`
}

type consumerModel struct {
	ID                   *string                  `json:"id"`
	Name                 *string                  `json:"name"`
	SourceID             *string                  `json:"sourceId"`
	Description          *string                  `json:"description"`
	License              *string                  `json:"license"`
	CreateTimeSinceEpoch *string                  `json:"createTimeSinceEpoch"`
	CustomProperties     map[string]consumerValue `json:"customProperties"`
	ArtifactCounts       map[string]int           `json:"artifactCounts"`
}

type consumerArtifact struct {
	ID               *string                  `json:"id"`
	Name             *string                  `json:"name"`
	ArtifactType     *string                  `json:"artifactType"`
	MetricsType      *string                  `json:"metricsType"`
	URI              *string                  `json:"uri"`
	CustomProperties map[string]consumerValue `json:"customProperties"`
}

// Metadata payload value keys retain their shared API spelling in v1.
type consumerValue struct {
	MetadataType *string  `json:"metadataType"`
	StringValue  *string  `json:"string_value"`
	IntValue     *string  `json:"int_value"`
	DoubleValue  *float64 `json:"double_value"`
	BoolValue    *bool    `json:"bool_value"`
}

type consumerError struct {
	Code    *string `json:"code"`
	Message *string `json:"message"`
}

// TestModelCatalogV1Contract exercises real HTTP routing, controller parsing,
// service logic, DB-to-API mapping, and PostgreSQL repository queries. No
// provider, HTTP response, pagination, or filtering implementation is mocked.
func TestModelCatalogV1Contract(t *testing.T) {
	server := newContractServer(t)
	alphaPath := v1Root + "/sources/curated/models/alpha-chat"

	t.Run("sources and labels", func(t *testing.T) {
		sources := walkPages[consumerSource](t, server, v1Root+"/sources", url.Values{
			"pageSize": {"1"}, "orderBy": {"NAME"}, "sortOrder": {"ASC"},
		}, 1)
		var ids []string
		for _, source := range sources {
			ids = append(ids, requiredValue(t, source.ID))
			require.NotEmpty(t, requiredValue(t, source.Name))
			require.NotNil(t, source.Labels)
			require.True(t, requiredValue(t, source.Enabled))
		}
		require.Equal(t, []string{"curated", "community", "empty"}, ids)
		require.Equal(t, []string{"validated"}, sources[0].Labels)

		filtered := walkPages[consumerSource](t, server, v1Root+"/sources", url.Values{"name": {"COMMUNITY"}}, 10)
		require.Len(t, filtered, 1)
		require.Equal(t, "community", requiredValue(t, filtered[0].ID))
		empty := walkPages[consumerSource](t, server, v1Root+"/sources", url.Values{"name": {"no-such-source"}}, 10)
		require.Empty(t, empty)

		labels := walkPages[struct {
			Name *string `json:"name"`
		}](t, server, v1Root+"/labels", url.Values{"pageSize": {"1"}, "orderBy": {"name"}, "sortOrder": {"ASC"}}, 1)
		require.Len(t, labels, 2)
		require.Equal(t, "community", requiredValue(t, labels[0].Name))
		require.Equal(t, "validated", requiredValue(t, labels[1].Name))
	})

	t.Run("model fields and source isolation", func(t *testing.T) {
		curated := getJSON[consumerModel](t, server, alphaPath, http.StatusOK)
		requireModel(t, curated)
		require.Equal(t, "alpha-chat", requiredValue(t, curated.Name))
		require.Equal(t, "curated", requiredValue(t, curated.SourceID))
		require.Equal(t, "Curated conversational model", requiredValue(t, curated.Description))
		require.Equal(t, "apache-2.0", requiredValue(t, curated.License))
		require.Equal(t, "1700000000000", requiredValue(t, curated.CreateTimeSinceEpoch))
		require.Equal(t, "MetadataStringValue", requiredValue(t, curated.CustomProperties["family"].MetadataType))
		require.Equal(t, "chat", requiredValue(t, curated.CustomProperties["family"].StringValue))
		require.Equal(t, map[string]int{"model-artifact": 2, "performance-metrics": 1, "accuracy-metrics": 1}, curated.ArtifactCounts)

		community := getJSON[consumerModel](t, server, v1Root+"/sources/community/models/alpha-chat", http.StatusOK)
		requireModel(t, community)
		require.Equal(t, "community", requiredValue(t, community.SourceID))
		require.Equal(t, "Community conversational model", requiredValue(t, community.Description))
		require.NotEqual(t, requiredValue(t, curated.ID), requiredValue(t, community.ID))
		// Counts are optional when there are no artifacts, not a required empty map.
		require.Empty(t, community.ArtifactCounts)
	})

	t.Run("slash and colon model names", func(t *testing.T) {
		for _, name := range []string{"org/charlie:v1.0", url.PathEscape("org/charlie:v1.0")} {
			path := v1Root + "/sources/curated/models/" + name
			result := getJSON[consumerModel](t, server, path, http.StatusOK)
			requireModel(t, result)
			require.Equal(t, "org/charlie:v1.0", requiredValue(t, result.Name))
			artifacts := walkPages[consumerArtifact](t, server, path+"/artifacts", nil, 10)
			require.Len(t, artifacts, 1)
			require.Equal(t, "nested-weights", requiredValue(t, artifacts[0].Name))
		}
	})

	t.Run("models pagination preserves all identities and ordering", func(t *testing.T) {
		// Every model has the same creation timestamp. The second case protects
		// cursor tie-breaking: a page boundary must not lose equal-sort-key rows.
		for _, orderBy := range []string{"ID", "CREATE_TIME"} {
			t.Run(orderBy, func(t *testing.T) {
				items := walkPages[consumerModel](t, server, v1Root+"/models", url.Values{
					"pageSize": {"2"}, "orderBy": {orderBy}, "sortOrder": {"ASC"},
				}, 2)
				var identities []string
				seen := map[string]bool{}
				for _, item := range items {
					requireModel(t, item)
					identity := requiredValue(t, item.SourceID) + ":" + requiredValue(t, item.Name)
					require.False(t, seen[identity], "duplicate identity across pages: %s", identity)
					seen[identity] = true
					identities = append(identities, identity)
				}
				require.Equal(t, []string{"curated:bravo-code", "curated:alpha-chat", "curated:org/charlie:v1.0", "community:alpha-chat", "community:delta-chat"}, identities)
			})
		}
	})

	t.Run("models filtering search sorting and empty results", func(t *testing.T) {
		for _, tc := range []struct {
			name     string
			query    url.Values
			expected []string
		}{
			{"source ascending", url.Values{"source": {"curated"}, "orderBy": {"NAME"}, "sortOrder": {"ASC"}}, []string{"alpha-chat", "bravo-code", "org/charlie:v1.0"}},
			{"source descending", url.Values{"source": {"curated"}, "orderBy": {"NAME"}, "sortOrder": {"DESC"}}, []string{"org/charlie:v1.0", "bravo-code", "alpha-chat"}},
			{"source label", url.Values{"sourceLabel": {"validated"}, "orderBy": {"NAME"}, "sortOrder": {"ASC"}}, []string{"alpha-chat", "bravo-code", "org/charlie:v1.0"}},
			{"case insensitive search", url.Values{"source": {"curated"}, "q": {"CONVERSATIONAL"}}, []string{"alpha-chat"}},
			{"property filter", url.Values{"filterQuery": {`license = 'mit'`}, "orderBy": {"ID"}, "sortOrder": {"ASC"}}, []string{"alpha-chat", "delta-chat"}},
			{"empty source", url.Values{"source": {"empty"}}, []string{}},
			{"unmatched search", url.Values{"q": {"no-such-model"}}, []string{}},
			{"unmatched label", url.Values{"sourceLabel": {"no-such-label"}}, []string{}},
		} {
			t.Run(tc.name, func(t *testing.T) {
				tc.query.Set("pageSize", "1")
				items := walkPages[consumerModel](t, server, v1Root+"/models", tc.query, 1)
				names := make([]string, 0, len(items))
				for _, item := range items {
					requireModel(t, item)
					names = append(names, requiredValue(t, item.Name))
					if tc.query.Get("source") == "curated" || tc.query.Get("sourceLabel") == "validated" {
						require.Equal(t, "curated", requiredValue(t, item.SourceID))
					}
				}
				require.Equal(t, tc.expected, names)
			})
		}
	})

	t.Run("artifact union payloads pagination and type filters", func(t *testing.T) {
		items := walkPages[consumerArtifact](t, server, alphaPath+"/artifacts", url.Values{
			"pageSize": {"2"}, "orderBy": {"ID"}, "sortOrder": {"ASC"},
		}, 2)
		require.Len(t, items, 4)
		var names []string
		for _, item := range items {
			require.NotEmpty(t, requiredValue(t, item.ID))
			names = append(names, requiredValue(t, item.Name))
			switch requiredValue(t, item.ArtifactType) {
			case "model-artifact":
				require.NotEmpty(t, requiredValue(t, item.URI))
				require.Nil(t, item.MetricsType)
			case "metrics-artifact":
				require.Contains(t, []string{"performance-metrics", "accuracy-metrics"}, requiredValue(t, item.MetricsType))
			default:
				t.Fatalf("unexpected artifactType: %s", *item.ArtifactType)
			}
		}
		require.Equal(t, []string{"weights-a", "weights-b", "latency-benchmark", "quality-benchmark"}, names)
		require.Equal(t, "oci://example.test/alpha:1", requiredValue(t, items[0].URI))
		require.Equal(t, "MetadataStringValue", requiredValue(t, items[0].CustomProperties["format"].MetadataType))
		require.Equal(t, "safetensors", requiredValue(t, items[0].CustomProperties["format"].StringValue))

		weights := walkPages[consumerArtifact](t, server, alphaPath+"/artifacts", url.Values{"artifactType": {"model-artifact"}, "pageSize": {"1"}}, 1)
		require.Len(t, weights, 2)
		for _, item := range weights {
			require.Equal(t, "model-artifact", requiredValue(t, item.ArtifactType))
		}
		performance := walkPages[consumerArtifact](t, server, alphaPath+"/artifacts/performance", nil, 10)
		require.Len(t, performance, 1)
		require.Equal(t, "metrics-artifact", requiredValue(t, performance[0].ArtifactType))
		require.Equal(t, "performance-metrics", requiredValue(t, performance[0].MetricsType))
		require.Equal(t, "MetadataDoubleValue", requiredValue(t, performance[0].CustomProperties["ttft_p90"].MetadataType))
		require.Equal(t, 12.5, requiredValue(t, performance[0].CustomProperties["ttft_p90"].DoubleValue))
		empty := walkPages[consumerArtifact](t, server, v1Root+"/sources/community/models/alpha-chat/artifacts", nil, 10)
		require.Empty(t, empty)
	})

	t.Run("filter options", func(t *testing.T) {
		result := getJSON[struct {
			Filters map[string]struct {
				Type   *string  `json:"type"`
				Values []string `json:"values"`
			} `json:"filters"`
		}](t, server, v1Root+"/models/filter_options", http.StatusOK)
		require.Contains(t, result.Filters, "license")
		require.Equal(t, "string", requiredValue(t, result.Filters["license"].Type))
		require.ElementsMatch(t, []string{"apache-2.0", "mit"}, result.Filters["license"].Values)
	})

	t.Run("not found errors", func(t *testing.T) {
		for _, path := range []string{
			v1Root + "/sources/curated/models/no-such-model",
			v1Root + "/sources/no-such-source/models/alpha-chat",
		} {
			result := getJSON[consumerError](t, server, path, http.StatusNotFound)
			require.Equal(t, "Not Found", requiredValue(t, result.Code))
			require.NotEmpty(t, requiredValue(t, result.Message))
		}
	})

	t.Run("invalid queries return JSON bad request", func(t *testing.T) {
		for _, endpoint := range []string{v1Root + "/models", v1Root + "/sources", v1Root + "/labels", alphaPath + "/artifacts", alphaPath + "/artifacts/performance"} {
			for _, query := range []url.Values{
				{"pageSize": {"0"}}, {"pageSize": {"-1"}}, {"pageSize": {"many"}},
				{"pageSize": {"2147483648"}}, {"nextPageToken": {"not-a-cursor!"}},
			} {
				t.Run(endpoint+"?"+query.Encode(), func(t *testing.T) {
					result := getJSON[consumerError](t, server, endpoint+"?"+query.Encode(), http.StatusBadRequest)
					require.Equal(t, "Bad Request", requiredValue(t, result.Code))
					require.NotEmpty(t, requiredValue(t, result.Message))
				})
			}
		}
		for _, query := range []url.Values{
			{"orderBy": {"NOT_A_FIELD"}}, {"source": {"curated"}, "sourceLabel": {"validated"}},
		} {
			result := getJSON[consumerError](t, server, v1Root+"/models?"+query.Encode(), http.StatusBadRequest)
			require.Equal(t, "Bad Request", requiredValue(t, result.Code))
			require.NotEmpty(t, requiredValue(t, result.Message))
		}
	})
}

func newContractServer(t *testing.T) *httptest.Server {
	t.Helper()
	db, cleanup := testutils.SetupPostgresWithMigrations(t, testhelpers.MustDatastoreSpec(t))
	t.Cleanup(cleanup)
	modelTypeID := testhelpers.GetCatalogModelTypeIDForDBTest(t, db)
	modelArtifactTypeID := testhelpers.GetCatalogModelArtifactTypeIDForDBTest(t, db)
	metricsArtifactTypeID := testhelpers.GetCatalogMetricsArtifactTypeIDForDBTest(t, db)
	sourceTypeID := testhelpers.GetCatalogSourceTypeIDForDBTest(t, db)

	svcs := modelcatalog.Services{
		CatalogModelRepository: modelservice.NewCatalogModelRepository(db, modelTypeID),
		CatalogArtifactRepository: service.NewCatalogArtifactRepository(db, map[string]int32{
			service.CatalogModelArtifactTypeName: modelArtifactTypeID, service.CatalogMetricsArtifactTypeName: metricsArtifactTypeID,
		}),
		CatalogModelArtifactRepository:   modelservice.NewCatalogModelArtifactRepository(db, modelArtifactTypeID),
		CatalogMetricsArtifactRepository: modelservice.NewCatalogMetricsArtifactRepository(db, metricsArtifactTypeID),
		CatalogSourceRepository:          service.NewCatalogSourceRepository(db, sourceTypeID),
		PropertyOptionsRepository:        service.NewPropertyOptionsRepository(db),
	}
	sources := modelcatalog.NewSourceCollection()
	require.NoError(t, sources.Merge("contract-fixture", map[string]basecatalog.ModelSource{
		"curated":   {CatalogSource: model.CatalogSource{Id: "curated", Name: "Alpha Curated", Labels: []string{"validated"}}},
		"community": {CatalogSource: model.CatalogSource{Id: "community", Name: "Beta Community", Labels: []string{"community"}}},
		"empty":     {CatalogSource: model.CatalogSource{Id: "empty", Name: "Gamma Empty"}},
	}))
	labels := modelcatalog.NewLabelCollection()
	require.NoError(t, labels.Merge("contract-fixture", []map[string]any{{"name": "validated"}, {"name": "community"}}))

	modelIDs := map[string]*int32{}
	// TestMain shares a DB across runs. Remove only this fixture's models (and
	// their artifacts) so go test -count=N does not accumulate test data.
	t.Cleanup(func() {
		for _, id := range modelIDs {
			require.NoError(t, svcs.CatalogModelRepository.DeleteByID(*id))
		}
	})
	for _, fixture := range []struct{ source, name, description, license string }{
		{"curated", "bravo-code", "Code generation model", "apache-2.0"},
		{"curated", "alpha-chat", "Curated conversational model", "apache-2.0"},
		{"curated", "org/charlie:v1.0", "Nested versioned model", "apache-2.0"},
		{"community", "alpha-chat", "Community conversational model", "mit"},
		{"community", "delta-chat", "Another community model", "mit"},
	} {
		key := fixture.source + ":" + fixture.name
		saved, err := svcs.CatalogModelRepository.Save(&models.CatalogModelImpl{
			TypeID: &modelTypeID,
			Attributes: &models.CatalogModelAttributes{
				Name: &key, CreateTimeSinceEpoch: new(int64(1700000000000)), LastUpdateTimeSinceEpoch: new(int64(1700000000001)),
			},
			Properties: &[]dbmodels.Properties{
				dbmodels.NewStringProperty("source_id", fixture.source, false),
				dbmodels.NewStringProperty("description", fixture.description, false),
				dbmodels.NewStringProperty("license", fixture.license, false),
			},
			CustomProperties: &[]dbmodels.Properties{dbmodels.NewStringProperty("family", "chat", true)},
		})
		require.NoError(t, err)
		modelIDs[key] = saved.GetID()
	}
	for _, fixture := range []struct{ model, name, uri string }{
		{"curated:alpha-chat", "weights-a", "oci://example.test/alpha:1"},
		{"curated:alpha-chat", "weights-b", "s3://example.test/alpha/weights"},
		{"curated:org/charlie:v1.0", "nested-weights", "oci://example.test/charlie:1"},
	} {
		_, err := svcs.CatalogModelArtifactRepository.Save(&models.CatalogModelArtifactImpl{
			TypeID: &modelArtifactTypeID,
			Attributes: &models.CatalogModelArtifactAttributes{
				Name: &fixture.name, URI: &fixture.uri, ArtifactType: new("model-artifact"),
			},
			CustomProperties: &[]dbmodels.Properties{dbmodels.NewStringProperty("format", "safetensors", true)},
		}, modelIDs[fixture.model])
		require.NoError(t, err)
	}
	for _, fixture := range []struct {
		name string
		kind models.MetricsType
	}{
		{"latency-benchmark", models.MetricsTypePerformance},
		{"quality-benchmark", models.MetricsTypeAccuracy},
	} {
		_, err := svcs.CatalogMetricsArtifactRepository.Save(&models.CatalogMetricsArtifactImpl{
			TypeID: &metricsArtifactTypeID,
			Attributes: &models.CatalogMetricsArtifactAttributes{
				Name: &fixture.name, ArtifactType: new("metrics-artifact"), MetricsType: fixture.kind,
			},
			CustomProperties: &[]dbmodels.Properties{
				dbmodels.NewDoubleProperty("ttft_p90", 12.5, true),
				dbmodels.NewDoubleProperty("requests_per_second", 100, true),
				dbmodels.NewIntProperty("hardware_count", 1, true),
				dbmodels.NewStringProperty("hardware_type", "GPU", true),
			},
		}, modelIDs["curated:alpha-chat"])
		require.NoError(t, err)
	}
	require.NoError(t, svcs.PropertyOptionsRepository.Refresh(sharedmodels.ContextPropertyOptionType))
	require.NoError(t, svcs.PropertyOptionsRepository.Refresh(sharedmodels.ArtifactPropertyOptionType))

	provider := modelcatalog.NewDBCatalog(svcs, sources)
	apiService := v1.NewModelCatalogServiceAPIService(provider, sources, nil, nil, labels, svcs.CatalogSourceRepository)
	server := httptest.NewServer(v1.NewRouter(v1.NewModelCatalogServiceAPIController(apiService)))
	server.Client().Timeout = 5 * time.Second
	t.Cleanup(server.Close)
	return server
}

func getJSON[T any](t *testing.T, server *httptest.Server, path string, status int) T {
	t.Helper()
	response, err := server.Client().Get(server.URL + path)
	require.NoError(t, err)
	defer func() { require.NoError(t, response.Body.Close()) }()
	body, err := io.ReadAll(io.LimitReader(response.Body, 1<<20))
	require.NoError(t, err)
	require.Equal(t, status, response.StatusCode, "GET %s: %s", path, body)
	contentType, _, err := mime.ParseMediaType(response.Header.Get("Content-Type"))
	require.NoError(t, err)
	require.Equal(t, "application/json", contentType)
	var result T
	require.NoError(t, json.Unmarshal(body, &result), "GET %s: %s", path, body)
	return result
}

func walkPages[T any](t *testing.T, server *httptest.Server, path string, query url.Values, pageSize int) []T {
	t.Helper()
	// Copy parameters; the returned opaque token is the only changing input.
	params := url.Values{}
	for key, values := range query {
		params[key] = append([]string(nil), values...)
	}
	items := make([]T, 0)
	seenTokens := map[string]bool{}
	for range 16 {
		page := getJSON[consumerPage[T]](t, server, path+"?"+params.Encode(), http.StatusOK)
		require.NotNil(t, page.Items, "items must be an array, including when empty")
		require.Equal(t, pageSize, requiredValue(t, page.PageSize))
		require.Equal(t, len(page.Items), requiredValue(t, page.Size), "size is the current page count, not a total")
		require.LessOrEqual(t, len(page.Items), pageSize)
		items = append(items, page.Items...)
		token := requiredValue(t, page.NextPageToken)
		if token == "" {
			return items
		}
		require.NotEmpty(t, page.Items, "a nonterminal page must make progress")
		require.False(t, seenTokens[token], "pagination token repeated")
		seenTokens[token] = true
		params.Set("nextPageToken", token)
	}
	t.Fatal("pagination did not terminate within the fixture's page bound")
	return nil
}

func requiredValue[T any](t *testing.T, value *T) T {
	t.Helper()
	require.NotNil(t, value, "consumer-required field is missing or null")
	return *value
}

func requireModel(t *testing.T, item consumerModel) {
	t.Helper()
	require.NotEmpty(t, requiredValue(t, item.Name))
	require.NotEmpty(t, requiredValue(t, item.SourceID))
	id, err := strconv.ParseUint(requiredValue(t, item.ID), 10, 32)
	require.NoError(t, err, "v1 model IDs are numeric strings")
	require.Positive(t, id)
}
