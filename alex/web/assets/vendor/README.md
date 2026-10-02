# Third-party assets

Served from this deployment so the client needs no CDN. Reproduce with
`python scripts/vendor_assets.py`; hashes are pinned in `vendor.lock.json`.

| Asset | Version | Licence | Source | Files |
|---|---|---|---|---|
| [Leaflet](https://leafletjs.com) | 1.9.4 | BSD-2-Clause | https://cdn.jsdelivr.net/npm/leaflet@1.9.4/ | 8 |
| [Plotly.js (cartesian bundle)](https://plotly.com/javascript/) | 4.1.1 | MIT | https://cdn.jsdelivr.net/npm/plotly.js-cartesian-dist-min@4.1.1/ | 2 |
| [PT Serif](https://fontsource.org/fonts/pt-serif) | 5.3.0 | OFL-1.1 | https://cdn.jsdelivr.net/npm/@fontsource/pt-serif@5.3.0/ | 13 |
| [PT Sans](https://fontsource.org/fonts/pt-sans) | 5.3.0 | OFL-1.1 | https://cdn.jsdelivr.net/npm/@fontsource/pt-sans@5.3.0/ | 13 |

## sha256

```
7891a86f2a41025e4ee327e3542e2499d9ac71f9adaf3411731480c03ef8ed58  fonts/LICENSE-PT-Sans.txt
28d2f6e154d4860524f6d5f7851c9a3b69492b82642e282381260bee7ab2f0a4  fonts/LICENSE-PT-Serif.txt
c3909442456a8f357aa2adf5caa11641ff935701be8bedc35188003aaa9cb6c9  fonts/pt-sans-cyrillic-400-italic.woff2
f283e584f001db04b39f339e859454a205c431c576c6a33af9259e54415db1bd  fonts/pt-sans-cyrillic-400-normal.woff2
ebd01c6db58e49690f2a790dbaae3ac8d9416036adb4349e23c677aaae3fff81  fonts/pt-sans-cyrillic-700-italic.woff2
05795f7954ba178292fce413dc9ac82838415d4d6f7e6afbce2eeefa34f461d1  fonts/pt-sans-cyrillic-700-normal.woff2
1f275d522571c81eb7d02d47015bf4eb1c60ab7520477bd40ba39425c1df49f2  fonts/pt-sans-latin-400-italic.woff2
2dea6190c113af617923c6b71f7f10ffbdf72074556f79963610254fe40e49be  fonts/pt-sans-latin-400-normal.woff2
064f8ebfc7134cdf0da9f34199ae15cbe100ef406d85c0187246a7bd7c57d5b6  fonts/pt-sans-latin-700-italic.woff2
05ea99a48ece3c624fd9df15f5cf4e1d94703bde9ed2b384495900f55e28befe  fonts/pt-sans-latin-700-normal.woff2
bba5da2e7ad198d906825a4fd919b22497744ee029f010b21add115dd73c28da  fonts/pt-sans-latin-ext-400-italic.woff2
94dc178a50c462bb9b38f4927cdbb17834993053c9d491690d72c3f2092c00e1  fonts/pt-sans-latin-ext-400-normal.woff2
1c861558c6ea049bc792a0fd8c4c257db5637cb371d9369b40da9fb84445c97f  fonts/pt-sans-latin-ext-700-italic.woff2
7e3cada9d8a24e34ffc07f3e8f5c864cc64d1555bce5153f42af8801e8cbe48a  fonts/pt-sans-latin-ext-700-normal.woff2
9d1c80c9a947548f5da15b917f693b2b1df148e807c212cce877793d326149c4  fonts/pt-serif-cyrillic-400-italic.woff2
050aee25e3462f72c4d357ee964b8df1801e701bae8af275b697581a87c04a48  fonts/pt-serif-cyrillic-400-normal.woff2
508410c557a2fb749fc8354927b9641a7f7f416768bdd633c978f0ef2b5f663e  fonts/pt-serif-cyrillic-700-italic.woff2
bec4e724c49bde6d6b3f15038c8015f5dc7c810bdaadd3aee537ab72afc50e4a  fonts/pt-serif-cyrillic-700-normal.woff2
cb373bde18855c82a0ebf2946ea661ebd0be58a7fbabdf20f7744ecd9c0a9cfd  fonts/pt-serif-latin-400-italic.woff2
4271064a37f3ffc0aac5f3806db8a72acc23e19447d1804e4e80d8796cbf6330  fonts/pt-serif-latin-400-normal.woff2
3cb3cfab3c562cbbb5a53accf433f65ed1cd0403ea3bdd6ceeb73bf87f23521c  fonts/pt-serif-latin-700-italic.woff2
bf23a7a4eebedbb87d4084a69496b29815914a18e339a00f5dc73a03c9c9328f  fonts/pt-serif-latin-700-normal.woff2
e03d9090077c76165d69af4d266eccb7bad28091d03244a31def2a711d597062  fonts/pt-serif-latin-ext-400-italic.woff2
2d7cdb9a975f016db59cb42a61df1b21889842b3ed4e2226f270f4b1349cf704  fonts/pt-serif-latin-ext-400-normal.woff2
1798f8e1f6f84c3ade658b73de077c97c187d0076efc75e47eac0420d98c8536  fonts/pt-serif-latin-ext-700-italic.woff2
94cdabe31b8195f0691d18eda86ea1fed049be7cd21eea22c858ad6dd6cc306b  fonts/pt-serif-latin-ext-700-normal.woff2
53e8dc25862014e4324741ca18fbe3611e11d42ef69f59f86ea8c5389647d4cb  vendor/leaflet/LICENSE
066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf  vendor/leaflet/images/layers-2x.png
1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6  vendor/leaflet/images/layers.png
00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528  vendor/leaflet/images/marker-icon-2x.png
574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437  vendor/leaflet/images/marker-icon.png
264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da  vendor/leaflet/images/marker-shadow.png
a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6  vendor/leaflet/leaflet.css
db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a  vendor/leaflet/leaflet.js
8c45d9eaf50c2f72dd9c7ab9ef440788ba58a3538484b5c51c1f17d5128752e7  vendor/plotly/LICENSE
79542150855a52c567260e3137095dbe2b744e6d702a5cabe67da246942fba7a  vendor/plotly/plotly-cartesian.min.js
```
