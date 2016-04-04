import os
from tinydb import *
from operator import itemgetter
import operator
from uuid import uuid1
from bson.objectid import ObjectId


class TinyMongoClient(object):
    def __init__(self,foldername="tinydb"):
        self.foldername=foldername
        try:
            os.mkdir(foldername)
        except:
            pass
        
    def __getitem__(self,key):
        return TinyMongoDatabase(key,self.foldername)
        
    def close(self):
        pass
    
    def __getattr__(self,name):
        return TinyMongoDatabase(name,self.foldername)
    
class TinyMongoDatabase(object):
    def __init__(self,database,foldername):
        self.foldername=foldername
        self.tinydb = TinyDB(os.path.join(foldername,database+".json"))
    
    def __getattr__(self,name):
        return TinyMongoCollection(name,self)
        
class TinyMongoCollection(object):
    def __init__(self,table,parent=None):
        self.tablename = table
        self.table = None
        self.parent = parent
        self.query = Query()
        self.insert_one = self.insert
        self.insert_many = self.insert
        self.update_one = self.update
        self.update_many = self.update
    
    def __getattr__(self,name):
        if self.table is None:
            self.tablename+= "."+name
        return self
            
    def buildTable(self):
        self.table = self.parent.tinydb.table(self.tablename)
        
    def insert(self,data,**kwargs):
        if self.table is None:self.buildTable()
        if not type(data) is list:data=[data]
        eids = []
        for adat in data:
            if not "_id" in adat:
                theid = str(uuid1()).replace("-","")
                eids.append(theid)
                adat["_id"] = theid
            else:
                eids.append(adat["_id"])
            self.table.insert(adat)
        if len(eids)==1:
            return eids[0]
        return eids
        
    def parseQuery(self,query):
        if self.table is None:self.buildTable()
        cnt=0
        allcond = None
        for akey,avalue in query.items():
            #set the operator
            if type(avalue)==dict:
                theop=operator.__dict__[avalue.keys()[0]]
                avalue=avalue.values()[0]
            elif "ObjectId" in str(type(avalue)):
                theop=operator.eq
                avalue=str(avalue)
            #defalt to equals
            else:
                theop=operator.eq
                
            if cnt==0:
                allcond=(self.query[akey]==avalue)
            else:
                allcond=allcond & (theop(self.query[akey],avalue))
            cnt+=1
        if not allcond is None:self.lastcond = allcond
        else:self.lastcond={}
        return allcond
        
    def update(self,query,data,argsdict={},**kwargs):
        if self.table is None:self.buildTable()
        if "$set" in data:data = data["$set"]
        allcond = self.parseQuery(query)
        try:
            self.table.update(data,allcond)
        except:
            return False
        return True
        
    def find(self,query={},fields={}):
        if self.table is None:self.buildTable()
        allcond = self.parseQuery(query)
        if allcond is None:return TinyMongoCursor(self.table.all())
        return TinyMongoCursor(self.table.search(allcond))
        
    def find_one(self,query={},fields={}):
        if self.table is None:self.buildTable()
        allcond = self.parseQuery(query)
        if allcond is None:return self.table.get({})
        return self.table.get(allcond)
        
    def count():
        if self.table is None:self.buildTable()
        return self.table.count(self.lastcond)
        
        
class TinyMongoCursor(object):
    def __init__(self,cursordat):
        self.cursordat = cursordat
        self.cursorpos = 0
        if type(self.cursordat) is list:
            if len(self.cursordat)==0:
                self.currentrec= None
            else:
                self.currentrec=self.cursordat[self.cursorpos]
        else:
            self.currentrec = self.cursordat
            
    def __getitem__(self,key):
        if type(key) is int:
            return self.cursordat[key]
        return self.currentrec[key]

    def __contains__(self, item):
        if self.currentrec is None:
            return False
        if item in self.currentrec:
            return True
        return False

    def sort(self,field,direction=1):
        if not type(self.cursordat) is list:
            pass
        elif direction==-1:
            self.cursordat = sorted(self.cursordat,key=itemgetter(field), reverse=True)
        else:
            self.cursordat = sorted(self.cursordat,key=itemgetter(field))
        return self
        
    def next(self):
        self.cursorpos+=1
        self.currentrec=self.cursordat[self.cursorpos]
        
    def count(self):
        return len(self.cursordat)

if __name__=="__main__":
    try:
        os.remove("tinydb/pacemain.json")
        os.remove("tinydb/pacelog.json")
    except:
        pass
    tmc = TinyMongoClient()
    #db = tmc.pacemain
    atable = tmc.pacemain.userdb
    theid=atable.insert_one({"_id":"56255a2b8f28086c0b95e282", "username":"admin","password":"admin","database":"pacemain","module":"ui_WEBIDE","skin":"skyblue","active":"true","fwmod":"WEBIDE","subdomain":"yes","grouping":"pacesoftware"})
    btable = tmc.pacemain.idefiles
    theid=btable.insert_one({"_id":"56e9a62ce1382347395e5e89", "username" : "admin", "adminonly" : "false"})
    userinfo = btable.find_one({"_id":ObjectId("56e9a62ce1382347395e5e89")})
    print userinfo
    #print theid
    #atable.insert_one({"id":"test5","someid":"nosometest", "great":2})
    #atable.insert_one({"id":"test3","someid":"nosometest", "great":2})
    #atable.insert_one({"id":"test2","someid":"nosometest", "great":1})
    #atable.insert_one({"id":"test4","someid":"nosometest", "great":1})
    #atable.insert_one({"id":"test6","someid":"nosometest", "great":1})
    #cursor = atable.find({"someid":"nosometest","great":{"le":2}}).sort("id")
    #print cursor.cursordat
    #cursor = atable.find_one({"someid":"nosometest","great":{"le":-44}}).sort("id")
    #print cursor.cursordat
    #db = tmc.pacelog
    #table = db.pacemain
    #theid=table.insert_one({"id":"log1","logid":"loginfo", "boyhowdy":2})
    #cursor = atable.find({})
    #if "subdomain" in cursor:print cursor['subdomain']
    #print cursor.cursordat
    #upd = table.update({"boyhowdy":2},{"$set":{"logid":"ZZZNUF", "boyhowdy":99}})
    #print upd
    #cursor = table.find({})
    #print cursor['boyhowdy']
    
