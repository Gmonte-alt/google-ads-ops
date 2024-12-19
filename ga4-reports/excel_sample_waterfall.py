import xlwings as xw

# Step 1: Prepare the Data in Excel
# This part would typically be done in your existing Python application

# Open the Excel Workbook
wb = xw.Book('ga4-reports/output/waterfall_example.xlsx')
ws = wb.sheets['Sheet1']  # Replace 'Sheet1' with your target sheet name

# Write your data preparation code here
# For example, writing some dummy data:
data = [
    ['Category', 'Start', 'End', 'Net Change'],
    ['Sales', 1000, 1200, 200],
    ['Expenses', -300, -400, -100],
    ['Profit', 700, 800, 100],
]
ws.range('A1').value = data

# Step 2: Insert VBA Macro
# Write a VBA macro into the workbook
vba_code = """
Sub CreateWaterfallChart()
    Dim ws As Worksheet
    Set ws = ThisWorkbook.Sheets("Sheet1") ' Adjust to your sheet name

    Dim chartObj As ChartObject
    Set chartObj = ws.ChartObjects.Add(Left:=300, Width:=400, Top:=50, Height:=300)
    
    With chartObj.Chart
        .SetSourceData Source:=ws.Range("A2:D4") ' Adjust the range as needed
        .ChartType = xlWaterfall
        .HasTitle = False
        .ChartTitle.Text = "Waterfall Chart Example"
    End With
End Sub
"""

# Write the VBA code into the workbook's VBA module
wb.api.VBProject.VBComponents.Add(1).CodeModule.AddFromString(vba_code)

# Step 3: Run the VBA Macro
# Execute the VBA macro to create the waterfall chart
wb.macro('CreateWaterfallChart')()

# Save and close the workbook
wb.save('ga4-reports/output/waterfall_example.xlsx')
wb.close()
