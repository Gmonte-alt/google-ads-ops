# file name: ga4-reports/excel_workbook_summary_ga4_ad-platform_dataviz.py
# version: V000-000-001
# Notes: Creates multiple variations of the first data visualization of the purchase key metric

### Enhanced Python Script

## Now, I’ll provide the complete Python script that integrates with the updated VBA code and dynamically creates all four charts.

import win32com.client as win32
import os

# Paths to the workbooks
final_report_path = r'C:\Users\GilbertMontemayor\workspace\google-ads-ops\ga4-reports\output\ga4_summary_metrics_formatted.xlsx'

# Check if the final report file exists
if not os.path.exists(final_report_path):
    print(f"Error: Final report workbook not found at {final_report_path}. Please check the path.")
    exit(1)

# Define the enhanced VBA code with dynamic parameters
vba_code = """
Sub CreateCombinationChart(chartNumber As Integer, topPosition As Double)
    Dim ws As Worksheet
    Dim chartSheet As Worksheet
    Dim chartObj As ChartObject
    Dim myChart As Chart
    Dim seriesCount As Integer
    Dim i As Integer
    Dim dataRange As Range
    Dim isoWeeksRange As Range
    Dim metricColumn As Range
    Dim yearRange As Range
    Dim campaignGroupRange As Range
    Dim filteredRange As Range
    Dim currentYear As Integer
    Dim campaignGroup As String
    Dim seriesXValues As Range
    Dim seriesValues As Range
    Dim row As Range ' New variable to represent each row in filteredRange
    
    ' Set your worksheet containing the data
    Set ws = ThisWorkbook.Sheets("--DATA--WTD--")
    
    ' Define the range for ISO Weeks (X-axis values)
    Set isoWeeksRange = ws.Range(ws.Cells(1, 5), ws.Cells(1, 56)) ' Columns E to BD, Row 1
    Debug.Print "ISO Weeks Range: " & isoWeeksRange.Address
    
    ' Define the range for the metric "FF_Purchase_Event_Count"
    Set metricColumn = ws.Range("A:A") ' Assuming metric is in column A
    Set yearRange = ws.Range("B:B") ' Assuming Year is in column B
    Set campaignGroupRange = ws.Range("D:D") ' Assuming Campaign_Group is in column D

    ' Initialize filteredRange
    Set filteredRange = Nothing

    ' Determine groupings based on chartNumber
    Select Case chartNumber
        Case 1
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = "FF_Purchase_Event_Count" And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Non-Paid Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Grand Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 2
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = "FF_Purchase_Event_Count" And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Non-Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Paid Social + Display Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Paid Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 3
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = "FF_Purchase_Event_Count" And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "SEM Brand Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Direct") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Organic Search (SEO)")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
        Case 4
            For i = 3 To ws.Cells(ws.Rows.Count, "A").End(xlUp).Row
                If ws.Cells(i, 1).Value = "FF_Purchase_Event_Count" And _
                   ((ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Google Search - TNH Non-Brand") Or _
                    (ws.Cells(i, 2).Value = 2024 And ws.Cells(i, 4).Value = "Bing SEM Total") Or _
                    (ws.Cells(i, 2).Value = 2023 And ws.Cells(i, 4).Value = "Paid Total")) Then
                    If filteredRange Is Nothing Then
                        Set filteredRange = ws.Rows(i)
                    Else
                        Set filteredRange = Union(filteredRange, ws.Rows(i))
                    End If
                End If
            Next i
    End Select

    ' Debugging: Print filtered range address after loop
    If Not filteredRange Is Nothing Then
               Debug.Print "Filtered Range: " & filteredRange.Address
    Else
        Debug.Print "No rows matched the criteria."
    End If

    ' If there is no data to chart, exit the sub
    If filteredRange Is Nothing Then
        MsgBox "No data available for the specified filters.", vbExclamation
        Exit Sub
    End If

    ' Create a new worksheet for the chart if it doesn't exist
    On Error Resume Next
    Set chartSheet = ThisWorkbook.Sheets("WTD_CHART")
    On Error GoTo 0
    If chartSheet Is Nothing Then
        Set chartSheet = ThisWorkbook.Sheets.Add(After:=ThisWorkbook.Sheets(ThisWorkbook.Sheets.Count))
        chartSheet.Name = "WTD_CHART"
    End If

    ' Create the chart on the new worksheet with the specified top position
    Set chartObj = chartSheet.ChartObjects.Add(Left:=50, Width:=800, Top:=topPosition, Height:=400)
    chartObj.Name = "Combination Chart " & chartNumber
    Set myChart = chartObj.Chart
    
    ' Set the chart type to Column Clustered initially
    myChart.ChartType = xlColumnClustered
    
    ' Add each series to the chart from the filtered range
    Dim rowIndex As Integer
    rowIndex = 1 ' Initialize row index for debugging
    
    For Each row In filteredRange.Rows
        ' Define series XValues and Values ranges
        Set seriesXValues = isoWeeksRange
        Set seriesValues = row.Cells(1, 5).Resize(1, 52) ' Columns E to BD for the current row
        
        ' Debugging: Print series ranges
        Debug.Print "Series " & rowIndex & " XValues: " & seriesXValues.Address
        Debug.Print "Series " & rowIndex & " Values: " & seriesValues.Address
        
        ' Check if seriesValues contains valid data before adding
        If WorksheetFunction.Count(seriesValues) > 0 Then
            With myChart.SeriesCollection.NewSeries
                .Name = row.Cells(1, 4).Value & " " & row.Cells(1, 2).Value ' Campaign_Group + Year
                .XValues = seriesXValues
                .Values = seriesValues
                
                ' Determine the chart type for each series
                If row.Cells(1, 2).Value = 2024 Then
                    .ChartType = xlColumnStacked ' Use Stacked Column for 2024 data
                    .AxisGroup = xlPrimary ' Set all 2024 data on the primary axis
                ElseIf row.Cells(1, 2).Value = 2023 Then
                    .ChartType = xlLine ' Use Line for 2023 Grand Total
                    .AxisGroup = xlPrimary ' Use primary axis for consistent comparison
                End If
            End With
        Else
            Debug.Print "Series " & rowIndex & " contains no data and will not be added."
        End If
        rowIndex = rowIndex + 1 ' Increment row index for debugging
    Next row
    
    ' Set chart title and axis titles
    myChart.HasTitle = True
    myChart.ChartTitle.Text = "Combination Chart for FF_Purchase_Event_Count - Grouping " & chartNumber
    
    With myChart.Axes(xlCategory)
        .HasTitle = True
        .AxisTitle.Text = "ISO Weeks"
    End With
    
    With myChart.Axes(xlValue)
        .HasTitle = True
        .AxisTitle.Text = "Purchase Event Count"
    End With
    
    ' Adjust the primary axis group for all series
    For i = 1 To myChart.SeriesCollection.Count
        myChart.SeriesCollection(i).AxisGroup = xlPrimary
    Next i
    
    ' Optional: Adjust the chart legend and display properties
    myChart.HasLegend = True
    myChart.Legend.Position = xlLegendPositionBottom
    
    ' Optional: Customize the chart appearance as needed
    
    MsgBox "Combination Chart " & chartNumber & " created successfully on 'WTD_CHART' sheet!", vbInformation
End Sub
"""

# Open Excel application
excel_app = win32.Dispatch("Excel.Application")

# Set Excel to be visible (optional)
excel_app.Visible = True

# Open the Final Report Workbook
final_workbook = excel_app.Workbooks.Open(final_report_path)

# Insert the VBA code into the final workbook
try:
    new_module = final_workbook.VBProject.VBComponents.Add(1)  # Add a new standard module
    new_module.Name = "Module1"
    new_module.CodeModule.AddFromString(vba_code)
    print("VBA code successfully added to the final report.")
except Exception as e:
    print(f"Error while adding VBA code: {e}")

# Run the macro multiple times for different charts
try:
    for chart_num, top_position in enumerate([20, 450, 880, 1310], start=1):
        macro_name = f"{final_workbook.Name}!Module1.CreateCombinationChart"
        excel_app.Application.Run(macro_name, chart_num, top_position)
        print(f"Chart {chart_num} created successfully.")
except Exception as e:
    print(f"Error while running the macro: {e}")

# Save and close the final report workbook
final_workbook.Save()

# Close the Excel application
final_workbook.Close(True)
excel_app.Quit()

print("Process completed successfully.")
