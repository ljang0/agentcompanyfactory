# amazon_seller_mock Schema

**Deploy order**: 5 (alphabetical among all `*_mock` directories; `BASE_PORT=8000` → port 8005)  
**Base URL**: `http://172.17.46.46:8005/`  
**Go Endpoint**: `GET /go?sid=<sid>` → `{initial_state, current_state, state_diff}`  
**Inject**: `POST /post?sid=<sid>` with body `{"action":"set","state":{...}}`

## State Schema

| Key | Type | Description and default |
|-----|------|-------------------------|
| `seller` | `Seller` | Default seller account (`SELLER_001`) |
| `products` | `Product[]` | Product catalog; 35 defaults |
| `orders` | `Order[]` | Orders; 27 defaults |
| `messages` | `Message[]` | Buyer/seller messages; 9 defaults |
| `returns` | `Return[]` | Return requests; 5 defaults |
| `campaigns` | `Campaign[]` | Advertising campaigns; 4 defaults |
| `accountHealth` | `AccountHealth` | Account-health metrics |
| `feedback` | `Feedback[]` | Buyer feedback; 12 defaults |
| `salesData` | `SalesData` | Daily sales metrics and summaries |
| `payments` | `Payments` | Balance, disbursements, fees, transactions |
| `fbaInventory` | `FBAInventory` | IPI, storage, aging, restock, and inbound shipment data |
| `coupons` | `Coupon[]` | Coupons; 3 defaults |
| `pricingRules` | `PricingRule[]` | Pricing rules; 3 defaults |
| `notifications` | `Notification[]` | Seller notifications; 8 defaults |
| `settings` | `Settings` | Notification, shipping, and listing settings |

### Core entity fields

- Seller: `{ id, displayName, legalName, email, marketplace, planType, storeName, sellerId, registeredSince, accountHealthRating, notificationCount, unreadMessages }`
- Product: `{ id, asin, sku, title, brand, category, price, salePrice, costOfGoods, fulfillmentChannel, status, condition, availableQuantity, reservedQuantity, inboundQuantity, buyBoxOwner, buyBoxPrice, lowestPrice, rating, reviewCount, bulletPoints[], description, keywords, weight, dimensions, dateCreated, lastUpdated }`
- Order: `{ id, amazonOrderId, purchaseDate, lastUpdateDate, status, fulfillmentChannel, salesChannel, buyerName, buyerEmail, shippingAddress, items[], orderTotal, shippingFee, amazonFees, netProceeds, carrier, trackingNumber, shipByDate, deliverByDate, shippedDate }`
- Shipping address: `{ name, line1, line2, city, state, postalCode }`; order item: `{ productId, sku, title, quantity, itemPrice, itemTotal }`
- Message: `{ id, threadId, orderId, buyerName, subject, body, sender, timestamp, isRead, status, responseDeadline, attachments[] }`
- Return: `{ id, orderId, amazonOrderId, returnRequestDate, status, reason, buyerComments, items[], sellerNotes, resolution }`
- Feedback: `{ id, orderId, buyerName, rating, comment, date, hasResponse, sellerResponse, removalRequested }`
- Notification: `{ id, type, title, message, timestamp, isRead, actionUrl, category }`

### Advertising and pricing fields

- Campaign: `{ id, name, type, status, dailyBudget, startDate, endDate, targetingType, bidStrategy, metrics, adGroups[] }`
- Campaign metrics: `{ impressions, clicks, spend, sales, acos, roas, orders, ctr, cpc }`
- Ad group: `{ id, name, status, defaultBid, products[], keywords[] }`; keyword: `{ keyword, matchType, bid, status, impressions, clicks, spend, sales }`
- Coupon: `{ id, name, type, discount, budget, budgetUsed, startDate, endDate, status, targetProducts[], redemptions, clipCount }`
- PricingRule: `{ id, name, target, strategy, status, productsCount, minPrice, maxPrice }`

### Metrics, payments, inventory, and settings fields

- AccountHealth: `{ overallRating, accountHealthRating, customerServicePerformance, policyCompliance, shippingPerformance }`
- Daily sales snapshot: `{ date, orderedProductSales, unitsOrdered, totalOrderItems, pageViews, sessions, buyBoxPercentage, orderItemSessionPercentage }`
- SalesData: `{ dailySnapshots[], summary }`; `summary.last7Days` and `summary.last30Days` use `{orderedProductSales,unitsOrdered,totalOrderItems,averageSellingPrice}`, and `previousPeriod` uses `{orderedProductSales,unitsOrdered}`
- Payments: `{ currentBalance, nextDisbursementDate, nextDisbursementEstimate, recentDisbursements[], feeBreakdown, transactions[] }`; disbursement `{id,date,amount,status}`; transaction `{id,date,type,description,amount,orderId}`
- FBAInventory: `{ inventoryPerformanceIndex, storageLimits, inventoryAge[], restockSuggestions[], inboundShipments[] }`; age item `{productId,asin,title,daysInInventory,quantity,estimatedFee,ageCategory}`; restock item `{productId,asin,title,currentStock,recommendedQuantity,daysOfSupply,alert}`; shipment `{id,shipmentName,status,destination,createdDate,estimatedArrival,itemCount,receivedCount,items[]}` with item `{asin,sku,quantity,received}`
- Settings: `{ notificationPreferences, shippingSettings, listingDefaults }`; shipping `{defaultShippingService,handlingTime,returnAddress}`; return address `{name,line1,city,state,postalCode,country}`; listing defaults `{defaultCondition,defaultFulfillment}`

## Minimal Inject Example

```json
{
  "type": "chrome_open_url",
  "parameters": {
    "url": "http://172.17.46.46:8005/",
    "inject_state": true,
    "state_content": {
      "action": "set",
      "state": {
        "seller": {"id":"SELLER_001","displayName":"Evergreen Home Goods","legalName":"Evergreen Home Goods LLC","email":"seller@example.com","marketplace":"Amazon.com","planType":"Professional","storeName":"Evergreen Home Goods","sellerId":"A1EXAMPLE","registeredSince":"2020-01-01","accountHealthRating":850,"notificationCount":0,"unreadMessages":0},
        "products": [], "orders": [], "messages": [], "returns": [], "campaigns": [],
        "accountHealth": {}, "feedback": [], "salesData": {"dailySnapshots":[],"summary":{}},
        "payments": {}, "fbaInventory": {}, "coupons": [], "pricingRules": [], "notifications": [], "settings": {}
      }
    }
  }
}
```

## Observable State Changes (for LLM evaluation)

| User Action | State Field Changed |
|-------------|---------------------|
| Create, edit, close, or delete a listing | `products` (append/update/remove) |
| Inline-edit a listing price | `products[*].price`, and where supplied `products[*].lastUpdated` |
| Inline-edit listing quantity | `products[*].availableQuantity`, `products[*].lastUpdated` |
| Confirm shipment for one or multiple orders | `orders[*].status`, `carrier`, `trackingNumber`, `shippedDate`, `lastUpdateDate` |
| Cancel an order | `orders[*].status`, `orders[*].lastUpdateDate` |
| Create an FBA shipment from Inventory Planning | `orders` (prepends the UI's `{id,type:"FBA Shipment",product,quantity,status,createdDate}` record) |
| Open a message thread | matching thread `messages[*].isRead`; unanswered messages become answered; `seller.unreadMessages` recalculated |
| Reply or mark a message “no response needed” | `messages` (reply/status message appended), `seller.unreadMessages` recalculated |
| Approve a return | `returns[*].status`, `returns[*].resolution` |
| Deny a return | `returns[*].status`, `returns[*].resolution`, `returns[*].sellerNotes` |
| Save return notes | `returns[*].sellerNotes` |
| Create a campaign | `campaigns` (new item appended) |
| Enable/pause campaign or change its budget | `campaigns[*].status` or `campaigns[*].dailyBudget` |
| Enable/pause a campaign keyword | `campaigns[*].adGroups[*].keywords[*].status` |
| Respond to feedback | `feedback[*].hasResponse`, `feedback[*].sellerResponse` |
| Request feedback removal | `feedback[*].removalRequested`, `feedback[*].hasResponse` |
| Create, enable/pause, or delete a pricing rule | `pricingRules` |
| Mark one/all notifications read | `notifications[*].isRead`, `seller.notificationCount` |
| Save shipping settings | `settings.shippingSettings` |
| Toggle a notification preference | `settings.notificationPreferences.<key>` |

