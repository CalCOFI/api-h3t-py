#!/usr/bin/env Rscript
# generate R-side golden values for the python parity tests.
# run from the repo root:
#   Rscript scripts/generate_r_golden.R ../api-h3t tests/fixtures/r_golden.json

suppressPackageStartupMessages({
  library(jsonlite)
})

args <- commandArgs(trailingOnly = TRUE)
if (length(args) < 2) {
  stop("usage: generate_r_golden.R <path-to-api-h3t> <output-json>")
}
api_dir <- normalizePath(args[[1]], mustWork = TRUE)
out_path <- args[[2]]
source(file.path(api_dir, "h3t_query.R"))

# --- zoom_to_res ---------------------------------------------------------

z_grid <- c(
  -1, 0, 0.5, 1, 1.5, 2.2, 2.5, 3.4, 4.6, 5.8, 7.0,
  8.2, 9.4, 10.6, 11.8, 12, 13, 15, 20, 22, 22.5, 25
)
zoom_to_res_golden <- vapply(z_grid, zoom_to_res, integer(1))

# --- h3_edge_length_deg --------------------------------------------------

edge_golden <- vapply(1:10, h3_edge_length_deg, numeric(1))

# --- tile_bbox -----------------------------------------------------------

bbox_grid <- list(
  list(z = 0, x = 0, y = 0),
  list(z = 1, x = 0, y = 0),
  list(z = 1, x = 1, y = 1),
  list(z = 5, x = 3, y = 12),
  list(z = 8, x = 41, y = 100),
  list(z = 10, x = 163, y = 397),
  list(z = 14, x = 2613, y = 6361)
)
bbox_golden <- lapply(bbox_grid, function(t) {
  bb <- tile_bbox(t$z, t$x, t$y)
  c(t, bb)
})

# --- wrap_tile_sql snapshot ---------------------------------------------

sample_user_sql <- "SELECT hex_h3res5 AS cell_id, AVG(std_tally) AS value, COUNT(*) AS n FROM bio_obs WHERE species_id = 42 GROUP BY 1"
sample_bbox <- tile_bbox(5, 3, 12)
wrap_tile_has_n <- wrap_tile_sql(sample_user_sql, sample_bbox, has_n = TRUE,
                                 max_rows = 50000L,
                                 buffer_deg = h3_edge_length_deg(5L) * 1.5)
wrap_tile_no_n <- wrap_tile_sql(
  "SELECT hex_h3res5 AS cell_id, AVG(temperature) AS value FROM env_obs",
  sample_bbox, has_n = FALSE, max_rows = 50000L, buffer_deg = 0)

# --- wrap_stats_sql snapshot --------------------------------------------

wrap_stats <- wrap_stats_sql(sample_user_sql)

# --- write ---------------------------------------------------------------

out <- list(
  zoom_to_res = list(z = z_grid, res = zoom_to_res_golden),
  h3_edge_length_deg = list(r = 1:10, deg = edge_golden),
  tile_bbox = bbox_golden,
  wrap_tile_sql_has_n = wrap_tile_has_n,
  wrap_tile_sql_no_n = wrap_tile_no_n,
  wrap_stats_sql = wrap_stats
)
write_json(out, out_path, auto_unbox = TRUE, digits = NA, pretty = TRUE)
cat("wrote ", out_path, "\n", sep = "")
