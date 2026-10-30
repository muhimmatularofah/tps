"""
Siapkan data SLS untuk dashboard.

Membaca shapefile SLS Kota Surabaya, mengurai nomor RT/RW dari kolom nmsls,
lalu menyimpan dua file GeoParquet di folder data/:
  - sls_rt.parquet : poligon per RT (SLS)
  - sls_rw.parquet : poligon RW hasil gabungan (dissolve) RT per kelurahan

Pemakaian:
    python prep_sls.py path/ke/SLS_KOTA_SURABAYA.shp
"""
import re
import sys
from pathlib import Path

import geopandas as gpd

RT_RW = re.compile(r"RT\.?\s*0*(\d+)\D+RW\.?\s*0*(\d+)", re.I)


def norm_name(s):
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def main(shp_path: str, out_dir: str = "data"):
    out = Path(out_dir)
    out.mkdir(exist_ok=True)

    g = gpd.read_file(shp_path)
    if g.crs is None or g.crs.to_epsg() != 4326:
        g = g.set_crs(4326, allow_override=g.crs is None).to_crs(4326)
    g["geometry"] = g.geometry.make_valid()

    ext = g["nmsls"].fillna("").str.extract(RT_RW)
    g["rt"] = ext[0].astype("Int64")
    g["rw"] = ext[1].astype("Int64")
    g["kode_desa"] = (g["kdprov"].astype(str) + g["kdkab"].astype(str)
                      + g["kdkec"].astype(str) + g["kddesa"].astype(str))
    g["kec_key"] = g["nmkec"].map(norm_name)
    g["desa_key"] = g["nmdesa"].map(norm_name)

    rt = g[["kode_desa", "nmkec", "nmdesa", "kec_key", "desa_key",
            "nmsls", "rt", "rw", "geometry"]].copy()
    # sedikit penyederhanaan (~1 m) supaya peta ringan
    rt["geometry"] = rt.geometry.simplify(0.00001, preserve_topology=True)
    rt.to_parquet(out / "sls_rt.parquet")

    rw = (g[g["rw"].notna()]
          .dissolve(by=["kode_desa", "nmkec", "nmdesa", "kec_key", "desa_key", "rw"],
                    as_index=False, aggfunc={"rt": "count"})
          .rename(columns={"rt": "jumlah_rt"}))
    rw["geometry"] = rw.geometry.simplify(0.00001, preserve_topology=True)
    rw.to_parquet(out / "sls_rw.parquet")

    print(f"RT: {len(rt)} poligon ({rt['rw'].isna().sum()} non-RT, mis. sawah/makam)")
    print(f"RW: {len(rw)} poligon dari {rw['kode_desa'].nunique()} kelurahan")
    print(f"Tersimpan di {out.resolve()}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "data")
