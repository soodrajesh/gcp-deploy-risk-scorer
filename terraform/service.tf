resource "google_service_account" "app" {
  account_id   = "dr-app"
  display_name = "deploy-risk runtime"
}
resource "google_project_iam_member" "app_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.app.email}"
}
resource "google_project_iam_member" "app_logs_write" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.app.email}"
}

# The scorer reads no project data: the diff arrives in the request and is never stored or sent to a model.
resource "google_cloud_run_v2_service" "app" {
  count               = var.image != "" ? 1 : 0
  name                = "deploy-risk"
  location            = var.region
  deletion_protection = false
  labels              = { env = "demo", owner = "platform" }
  template {
    service_account                  = google_service_account.app.email
    timeout                          = "60s"
    max_instance_request_concurrency = 20
    scaling {
      min_instance_count = 0
      max_instance_count = 2
    }
    containers {
      image = var.image
      resources {
        limits = { cpu = "1", memory = "512Mi" }
      }
      startup_probe {
        http_get { path = "/healthz" }
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }
      env {
        name  = "DEPLOYRISK_LLM"
        value = "vertex"
      }
      env {
        name  = "DEPLOYRISK_ALLOWED_PROVIDERS"
        value = "vertex,none"
      }
    }
  }
  depends_on = [google_project_iam_member.app_vertex]
}

resource "google_cloud_run_v2_service_iam_member" "admin_invoker" {
  count    = var.image != "" ? 1 : 0
  name     = google_cloud_run_v2_service.app[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "user:${var.admin_email}"
}
