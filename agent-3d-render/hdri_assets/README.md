# HDRI assets

CC0 (public domain) HDRIs from [Poly Haven](https://polyhaven.com), 1k resolution (fine for
background + reflections at the resolutions this renderer works at; no attribution required by
the license, credited here anyway). Downloaded from `dl.polyhaven.org` on 2026-08-19.

These are pre-staged into S3 by Terraform (`aws_s3_object.hdri`, see `terraform/main.tf`) rather
than fetched from Poly Haven at render time -- keeps renders reproducible, off a third-party
runtime dependency, and reuses the existing deployment bucket/IAM grant. `agent.py`'s
`_ensure_hdri()` downloads from S3 (not Poly Haven) and verifies against the sha256 in
`blender_runtime.HDRI_CATALOG` before use.

| Catalog name | File | Source slug | sha256 |
|---|---|---|---|
| `clear_sky` | `kloofendal_43d_clear_puresky.hdr` | `kloofendal_43d_clear_puresky` | `de7ba9d0b070470dbb70d0144294c8708068df353537b26ab59c394707e84377` |
| `golden_sunset` | `industrial_sunset_puresky.hdr` | `industrial_sunset_puresky` | `ce8235e4b1b10b620120ceeb32eb3e80af15ea29b09a8625a4bdae647dff328d` |
| `overcast` | `overcast_soil_puresky.hdr` | `overcast_soil_puresky` | `2dbbbbb1323a8e8989db2e8306bd13099b215539e5adba41b85738a250a7904e` |
| `night_sky` | `moonless_golf.hdr` | `moonless_golf` | `4f597078024bd81429431e872d466d8808653ad62a8bc8c61d8052af7466c3aa` |
| `studio` | `studio_small_03.hdr` | `studio_small_03` | `30933d55e45f0795daf49f3cbefbe0e5ebcb821ee04fb0a2818c02ffc3938817` |
| `field` | `sunflowers_puresky.hdr` | `sunflowers_puresky` | `39a18be788fda30e1b1929d4ebd78b5da14433a6e2271eff1928a35e481c5111` |

Re-fetch or add more with:
```bash
curl -sL -o hdri_assets/<name>.hdr "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/1k/<slug>_1k.hdr"
shasum -a 256 hdri_assets/<name>.hdr
```
Then add an entry to `HDRI_CATALOG` in `blender_runtime.py` and an `aws_s3_object` block in
`terraform/main.tf`.
