#!/usr/bin/env python3
"""Join measured RTTs with explicit city-proxy great-circle distances, never cable lengths."""
import csv,json,math,pathlib,time,urllib.parse,urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[2];OUT=ROOT/'results/global'
PROXIES={
'ap-northeast-1':('Tokyo','Tokyo, Japan'),
'ap-northeast-2':('Seoul','Seoul, South Korea'),
'ap-northeast-3':('Osaka','Osaka, Japan'),
'ap-south-1':('Mumbai','Mumbai, India'),
'ap-southeast-1':('Singapore','Singapore'),
'ap-southeast-2':('Sydney','Sydney, Australia'),
'ca-central-1':('Montréal','Montreal, Quebec, Canada'),
'eu-central-1':('Frankfurt','Frankfurt am Main, Germany'),
'eu-north-1':('Stockholm','Stockholm, Sweden'),
'eu-west-1':('Dublin','Dublin, Ireland'),
'eu-west-2':('London','London, United Kingdom'),
'eu-west-3':('Paris','Paris, France'),
'sa-east-1':('São Paulo','Sao Paulo, Brazil'),
'us-east-1':('N. Virginia','Ashburn, Virginia, United States'),
'us-east-2':('Ohio','Columbus, Ohio, United States'),
'us-west-1':('N. California','San Francisco, California, United States'),
'us-west-2':('Oregon','Boardman, Oregon, United States')}
cache=OUT/'region-city-proxies.json';saved=json.loads(cache.read_text()) if cache.exists() else {};coords=saved.get('regions',{})
for region,(label,query) in PROXIES.items():
 if region in coords:continue
 url='https://nominatim.openstreetmap.org/search?'+urllib.parse.urlencode({'q':query,'format':'json','limit':1})
 request=urllib.request.Request(url,headers={'User-Agent':'AutomergeWebTransportBenchmark/0.1 (regional distance analysis)'})
 result=json.load(urllib.request.urlopen(request,timeout=30));assert result,'No city result for '+query;place=result[0]
 coords[region]={'label':label,'representative_city_query':query,'lat':float(place['lat']),'lon':float(place['lon']),'source_url':url,'osm_type':place['osm_type'],'osm_id':place['osm_id'],'display_name':place['display_name'],'licence':place['licence']}
 cache.write_text(json.dumps({'description':'Analyst-chosen representative cities within the named regions. These are not AWS facility coordinates.','coordinate_source':'OpenStreetMap contributors via Nominatim; ODbL 1.0','aws_region_names_source':'https://docs.aws.amazon.com/global-infrastructure/latest/regions/aws-regions.html','regions':coords},indent=2)+'\n');print('Located',region,label,flush=True);time.sleep(1.1)
def distance(a,b):
 lat1,lon1,lat2,lon2=map(math.radians,[a['lat'],a['lon'],b['lat'],b['lon']]);dlat=lat2-lat1;dlon=lon2-lon1;h=math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2;return 6371.0088*2*math.asin(math.sqrt(min(1,h)))
rows=[]
with (OUT/'matrix.csv').open() as file:
 for r in csv.DictReader(file):
  source,dest=r['source_region'],r['destination_region'];direct=distance(coords[source],coords[dest]);via=distance(coords[source],coords['us-west-1'])+distance(coords['us-west-1'],coords[dest])
  rows.append({'source_region':source,'source_label':coords[source]['label'],'destination_region':dest,'destination_label':coords[dest]['label'],'direct_great_circle_km':round(direct,1),'via_relay_great_circle_km':round(via,1),'p50_ms':float(r['p50_ms']) if r['p50_ms'] else None,'p95_ms':float(r['p95_ms']) if r['p95_ms'] else None,'p99_ms':float(r['p99_ms']) if r['p99_ms'] else None,'samples':int(r['samples']),'passed':int(r['passed']),'attempts':int(r['attempts'])})
metadata={'scope':'WebTransport only; no worldwide WebSocket comparison exists.','relay':'us-west-1','distance_method':'Haversine on a 6371.0088 km sphere using analyst-chosen OpenStreetMap city proxies. Both distance columns are one-way geographical lengths. Via-relay distance sums sender→San Francisco proxy→responder; RTT includes the return exchange.','limitations':'Not actual AWS datacenter, AZ, fibre, or measured packet-route distances. Same-region direct distance is 0 by convention; actual Lambda placements differ.','coordinate_attribution':'© OpenStreetMap contributors, ODbL 1.0; https://www.openstreetmap.org/copyright','measured_data':'matrix.csv: 3 repetitions × 10 RTTs per pair; 17 regions, 289 pairs'}
(OUT/'region-distance-benchmarks.json').write_text(json.dumps({'metadata':metadata,'regions':coords,'rows':rows},indent=2)+'\n')
with (OUT/'region-distance-benchmarks.csv').open('w',newline='') as file:
 writer=csv.DictWriter(file,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
print('Wrote all',len(rows),'region pairs with measured RTT and approximate distances')
